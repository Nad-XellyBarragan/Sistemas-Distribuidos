from concurrent import futures
import time
import threading
import json
import os
import argparse
import grpc

import tracker_pb2
import tracker_pb2_grpc

STATE_FILE = "tracker_state.json"

OFFLINE_TIMEOUT_SEC = 12          # para "EN LINEA/FUERA"
PURGE_AFTER_SEC = 5 * 60          # 5 min -> eliminar peer del estado
CLEANUP_EVERY_SEC = 30            # cada 30s corre el reloj


def fmt_bytes(n: int) -> str:
    x = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if x < 1024:
            return f"{x:.1f}{unit}"
        x /= 1024
    return f"{x:.1f}PB"


class TrackerService(tracker_pb2_grpc.TrackerServicer):
    """
    peers_by_torrent[ih][peer_id] = PeerInfo
    prog_by_torrent[ih][peer_id]  = {pieces_have,total_pieces,bytes_downloaded,status,last_seen}

    peers_global[peer_id] = {ip,port,last_seen}

    NEW:
    endpoint_index["ip:port"] = peer_id  (para deduplicar reinicios)
    torrents[ih] = {meta: TorrentEntry, torrent_json: str}
    """
    def __init__(self):
        self._lock = threading.Lock()

        self.peers_by_torrent = {}
        self.prog_by_torrent = {}
        self.torrent_names = {}

        self.peers_global = {}      # peer_id -> {ip, port, last_seen}
        self.endpoint_index = {}    # "ip:port" -> peer_id  (dedupe)
        self.torrents = {}          # info_hash -> {"meta": dict, "torrent_json": str}

    # ---------- persistence ----------
    def snapshot(self):
        with self._lock:
            peers_dump = {
                ih: {pid: {"peer_id": p.peer_id, "ip": p.ip, "port": p.port} for pid, p in m.items()}
                for ih, m in self.peers_by_torrent.items()
            }
            return {
                "peers_by_torrent": peers_dump,
                "prog_by_torrent": self.prog_by_torrent,
                "torrent_names": self.torrent_names,
                "peers_global": self.peers_global,
                "endpoint_index": self.endpoint_index,
                "torrents": self.torrents,
            }

    def save_state(self):
        snap = self.snapshot()
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(snap, f)
        except Exception:
            pass

    def load_state(self):
        if not os.path.isfile(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        with self._lock:
            self.peers_by_torrent = {}
            for ih, m in data.get("peers_by_torrent", {}).items():
                self.peers_by_torrent[ih] = {}
                for pid, p in m.items():
                    self.peers_by_torrent[ih][pid] = tracker_pb2.PeerInfo(
                        peer_id=p.get("peer_id", pid),
                        ip=p.get("ip", "?"),
                        port=int(p.get("port", 0)),
                    )

            self.prog_by_torrent = data.get("prog_by_torrent", {})
            self.torrent_names = data.get("torrent_names", {})
            self.peers_global = data.get("peers_global", {})
            self.endpoint_index = data.get("endpoint_index", {})
            self.torrents = data.get("torrents", {})

    # ---------- helpers ----------
    def _is_online(self, last_seen: float) -> bool:
        return (time.time() - float(last_seen)) <= OFFLINE_TIMEOUT_SEC

    def _ensure_peer_torrent(self, ih: str, p: tracker_pb2.PeerInfo, total_pieces: int, status: str):
        now = time.time()

        self.peers_by_torrent.setdefault(ih, {})
        self.peers_by_torrent[ih][p.peer_id] = p

        self.prog_by_torrent.setdefault(ih, {})
        self.prog_by_torrent[ih].setdefault(p.peer_id, {
            "pieces_have": 0,
            "total_pieces": int(total_pieces),
            "bytes_downloaded": 0,
            "status": status,
            "last_seen": now,
        })

        st = self.prog_by_torrent[ih][p.peer_id]
        if total_pieces:
            st["total_pieces"] = int(total_pieces)
        st["status"] = status
        st["last_seen"] = now

    def _merge_peer_ids(self, old_pid: str, new_pid: str):
        """
        Si el mismo endpoint ip:port llega con otro peer_id (reinicio),
        movemos estado del old_pid -> new_pid y borramos old_pid.
        """
        # 1) mover global si aplica
        if old_pid in self.peers_global and new_pid not in self.peers_global:
            self.peers_global[new_pid] = self.peers_global[old_pid]
        self.peers_global.pop(old_pid, None)

        # 2) por torrent: peers_by_torrent y prog_by_torrent
        for ih, peers_map in list(self.peers_by_torrent.items()):
            if old_pid in peers_map and new_pid not in peers_map:
                peers_map[new_pid] = peers_map[old_pid]
            peers_map.pop(old_pid, None)

        for ih, prog_map in list(self.prog_by_torrent.items()):
            if old_pid in prog_map and new_pid not in prog_map:
                prog_map[new_pid] = prog_map[old_pid]
            prog_map.pop(old_pid, None)

    def _remove_peer_everywhere(self, pid: str):
        """Elimina pid de estructuras globales y por torrent + endpoint_index."""
        # quitar global
        self.peers_global.pop(pid, None)

        # quitar de torrents
        for ih, pm in list(self.prog_by_torrent.items()):
            pm.pop(pid, None)
            if ih in self.peers_by_torrent:
                self.peers_by_torrent[ih].pop(pid, None)
            # limpia vacíos
            if ih in self.prog_by_torrent and not self.prog_by_torrent[ih]:
                self.prog_by_torrent.pop(ih, None)
            if ih in self.peers_by_torrent and not self.peers_by_torrent[ih]:
                self.peers_by_torrent.pop(ih, None)

        # quitar del índice endpoint -> pid
        for ep, ep_pid in list(self.endpoint_index.items()):
            if ep_pid == pid:
                self.endpoint_index.pop(ep, None)

    # ---------- NEW: global hello/heartbeat ----------
    def Hello(self, request, context):
        p = request.peer
        now = time.time()
        endpoint = f"{p.ip}:{int(p.port)}"

        with self._lock:
            old_pid = self.endpoint_index.get(endpoint)

            # Si el mismo endpoint llega con otro peer_id -> merge (reinicio)
            if old_pid and old_pid != p.peer_id:
                print(f"[DEDUP] endpoint={endpoint} old_peer={old_pid} new_peer={p.peer_id} -> merge")
                self._merge_peer_ids(old_pid, p.peer_id)

            self.endpoint_index[endpoint] = p.peer_id
            self.peers_global[p.peer_id] = {"ip": p.ip, "port": int(p.port), "last_seen": now}

        print(f"[HELLO] {endpoint} peer={p.peer_id}")
        self.save_state()
        return tracker_pb2.HelloResponse(ok=True)

    def Heartbeat(self, request, context):
        now = time.time()
        with self._lock:
            if request.peer_id in self.peers_global:
                self.peers_global[request.peer_id]["last_seen"] = now
        self.save_state()
        return tracker_pb2.HeartbeatResponse(ok=True)

    # ---------- NEW: torrent catalog ----------
    def PublishTorrent(self, request, context):
        ih = request.info_hash
        meta = request.meta
        raw = request.torrent_json

        with self._lock:
            self.torrents[ih] = {
                "meta": {
                    "info_hash": ih,
                    "torrent_name": meta.torrent_name,
                    "file_name": meta.file_name,
                    "pieces": int(meta.pieces),
                    "last_piece": int(meta.last_piece),
                },
                "torrent_json": raw.decode("utf-8", errors="ignore"),
            }
            if meta.torrent_name:
                self.torrent_names[ih] = meta.torrent_name

        print(f"[PUBLISH] torrent={meta.torrent_name or ih} file={meta.file_name} pieces={meta.pieces}")
        self.save_state()
        return tracker_pb2.PublishTorrentResponse(ok=True)

    def ListTorrents(self, request, context):
        with self._lock:
            out = []
            for ih, v in self.torrents.items():
                m = v.get("meta", {})
                out.append(tracker_pb2.TorrentEntry(
                    info_hash=ih,
                    torrent_name=m.get("torrent_name", ih),
                    file_name=m.get("file_name", ""),
                    pieces=int(m.get("pieces", 0)),
                    last_piece=int(m.get("last_piece", 0)),
                ))
        return tracker_pb2.ListTorrentsResponse(torrents=out)

    def GetTorrent(self, request, context):
        ih = request.info_hash
        with self._lock:
            v = self.torrents.get(ih)
            if not v:
                return tracker_pb2.GetTorrentResponse(torrent_json=b"")
            txt = v.get("torrent_json", "")
        return tracker_pb2.GetTorrentResponse(torrent_json=txt.encode("utf-8"))

    # ---------- existing ----------
    def Announce(self, request, context):
        ih = request.info_hash
        p = request.peer
        status = "seeder" if request.is_seeder else "leecher"

        with self._lock:
            # (opcional) también aseguramos dedupe del índice si announce llega antes que hello
            endpoint = f"{p.ip}:{int(p.port)}"
            old_pid = self.endpoint_index.get(endpoint)
            if old_pid and old_pid != p.peer_id:
                print(f"[DEDUP] (announce) endpoint={endpoint} old_peer={old_pid} new_peer={p.peer_id} -> merge")
                self._merge_peer_ids(old_pid, p.peer_id)
            self.endpoint_index[endpoint] = p.peer_id

            self._ensure_peer_torrent(ih, p, request.total_pieces, status)

            tname = request.torrent_name or ""
            if tname:
                self.torrent_names[ih] = tname

            st = self.prog_by_torrent[ih][p.peer_id]
            tp = int(st.get("total_pieces", 0) or 0)
            ph = int(st.get("pieces_have", 0) or 0)
            pct = (ph / tp * 100.0) if tp else 0.0

        shown = self.torrent_names.get(ih, ih)
        print(f"[ANNOUNCE] {p.ip}:{p.port} peer={p.peer_id} {ph}/{tp} ({pct:.1f}%) status={status} torrent={shown}")
        self.save_state()
        return tracker_pb2.AnnounceResponse(ok=True)

    def GetPeers(self, request, context):
        ih = request.info_hash
        with self._lock:
            peers_map = self.peers_by_torrent.get(ih, {})
            prog_map = self.prog_by_torrent.get(ih, {})

            seeders, others = [], []
            for pid, pinfo in peers_map.items():
                st = prog_map.get(pid, {})
                if not self._is_online(st.get("last_seen", 0)):
                    continue
                status = st.get("status", "leecher")
                (seeders if status == "seeder" else others).append(pinfo)

            out = seeders + others
        return tracker_pb2.GetPeersResponse(peers=out)

    def UpdateProgress(self, request, context):
        ih = request.info_hash
        pid = request.peer_id
        now = time.time()

        with self._lock:
            self.prog_by_torrent.setdefault(ih, {})
            self.prog_by_torrent[ih].setdefault(pid, {
                "pieces_have": 0,
                "total_pieces": 0,
                "bytes_downloaded": 0,
                "status": "leecher",
                "last_seen": now,
            })

            st = self.prog_by_torrent[ih][pid]
            prev_pieces = int(st.get("pieces_have", 0) or 0)
            prev_bytes = int(st.get("bytes_downloaded", 0) or 0)
            prev_status = str(st.get("status", "leecher"))

            st["pieces_have"] = int(request.pieces_have)
            st["bytes_downloaded"] = int(request.bytes_downloaded)
            st["status"] = request.status
            st["last_seen"] = now

            tp = int(st.get("total_pieces", 0) or 0)
            ph = int(st.get("pieces_have", 0) or 0)
            pct = (ph / tp * 100.0) if tp else 0.0

            changed = (ph != prev_pieces) or (st["bytes_downloaded"] != prev_bytes) or (st["status"] != prev_status)
            pinfo = self.peers_by_torrent.get(ih, {}).get(pid, tracker_pb2.PeerInfo(peer_id=pid, ip="?", port=0))

        if changed:
            shown = self.torrent_names.get(ih, ih)
            print(
                f"[PROGRESS] {pinfo.ip}:{pinfo.port} peer={pid} "
                f"{ph}/{tp} ({pct:.1f}%) down={fmt_bytes(int(request.bytes_downloaded))} "
                f"status={request.status} torrent={shown}"
            )

        self.save_state()
        return tracker_pb2.UpdateProgressResponse(ok=True)

    def GetProgress(self, request, context):
        ih = request.info_hash
        t = time.time()

        with self._lock:
            prog_map = self.prog_by_torrent.get(ih, {})
            peers_map = self.peers_by_torrent.get(ih, {})
            resp = []

            for pid, st in prog_map.items():
                pinfo = peers_map.get(pid, tracker_pb2.PeerInfo(peer_id=pid, ip="?", port=0))
                last_seen = float(st.get("last_seen", 0))
                online = (t - last_seen) <= OFFLINE_TIMEOUT_SEC

                resp.append(tracker_pb2.PeerProgress(
                    peer_id=pid,
                    ip=pinfo.ip,
                    port=pinfo.port,
                    pieces_have=int(st.get("pieces_have", 0) or 0),
                    total_pieces=int(st.get("total_pieces", 0) or 0),
                    bytes_downloaded=int(st.get("bytes_downloaded", 0) or 0),
                    status=str(st.get("status", "leecher")),
                    online=online,
                    last_seen_unix=int(last_seen),
                ))

        return tracker_pb2.GetProgressResponse(progress=resp)

    # ---------- cleanup clock ----------
    def cleanup_job(self):
        while True:
            time.sleep(CLEANUP_EVERY_SEC)
            now = time.time()
            removed = []

            with self._lock:
                # 1) limpiar global peers (y de paso todo)
                for pid, info in list(self.peers_global.items()):
                    last = float(info.get("last_seen", 0) or 0)
                    if now - last > PURGE_AFTER_SEC:
                        removed.append(("global", pid))
                        self._remove_peer_everywhere(pid)

                # 2) limpiar por torrent (por si hay peers sin global)
                for ih, pm in list(self.prog_by_torrent.items()):
                    for pid, st in list(pm.items()):
                        last = float(st.get("last_seen", 0) or 0)
                        if now - last > PURGE_AFTER_SEC:
                            removed.append((ih, pid))
                            # elimina de ese torrent (y limpia vacíos)
                            pm.pop(pid, None)
                            if ih in self.peers_by_torrent:
                                self.peers_by_torrent[ih].pop(pid, None)

                    if ih in self.prog_by_torrent and not self.prog_by_torrent[ih]:
                        self.prog_by_torrent.pop(ih, None)
                    if ih in self.peers_by_torrent and not self.peers_by_torrent[ih]:
                        self.peers_by_torrent.pop(ih, None)

                # 3) limpieza extra del índice endpoint_index para evitar basura
                valid_pids = set(self.peers_global.keys())
                for ep, pid in list(self.endpoint_index.items()):
                    if pid not in valid_pids:
                        self.endpoint_index.pop(ep, None)

            if removed:
                print(f"[CLEANUP] eliminados por >5min offline: {len(removed)}")
                self.save_state()

    # ---------- menu ----------
    def build_nodes_table(self) -> str:
        snap = self.snapshot()
        peers_by_torrent = snap.get("peers_by_torrent", {})
        prog_by_torrent = snap.get("prog_by_torrent", {})
        torrent_names = snap.get("torrent_names", {})
        peers_global = snap.get("peers_global", {})

        now = time.time()

        headers = ["IP:PUERTO", "ESTADO", "ROL", "TORRENTS", "PROGRESO"]
        rows = []

        # Consolidar por peer_id desde la info de torrents (si tiene)
        peer_torrents = {}
        peer_info = {}

        for ih, peers in peers_by_torrent.items():
            for pid, p in peers.items():
                peer_info[pid] = (p.get("ip", "?"), int(p.get("port", 0)))
                peer_torrents.setdefault(pid, set()).add(ih)

        for pid in sorted(peer_info.keys()):
            ip, port = peer_info.get(pid, ("?", 0))

            last_seen = 0.0
            role = "leecher"
            prog_list = []

            for ih in peer_torrents.get(pid, set()):
                st = prog_by_torrent.get(ih, {}).get(pid, {})
                last_seen = max(last_seen, float(st.get("last_seen", 0) or 0))
                if st.get("status") == "seeder":
                    role = "seeder"
                tp = int(st.get("total_pieces", 0) or 0)
                ph = int(st.get("pieces_have", 0) or 0)
                pct = (ph / tp * 100.0) if tp else 0.0
                prog_list.append(f"{torrent_names.get(ih, ih)} {ph}/{tp} ({pct:.0f}%)")

            estado = "EN LINEA" if (now - last_seen) <= OFFLINE_TIMEOUT_SEC else "FUERA"
            tnames = ", ".join([torrent_names.get(ih, ih) for ih in sorted(peer_torrents.get(pid, set()))]) or "-"
            prog_str = " | ".join(prog_list) if prog_list else "-"

            rows.append([f"{ip}:{port}", estado, role, tnames, prog_str])

        # También mostrar peers "solo hello" (sin torrents)
        for pid, info in peers_global.items():
            if pid in peer_info:
                continue
            ip = info.get("ip", "?")
            port = int(info.get("port", 0))
            last = float(info.get("last_seen", 0) or 0)
            estado = "EN LINEA" if (now - last) <= OFFLINE_TIMEOUT_SEC else "FUERA"
            rows.append([f"{ip}:{port}", estado, "-", "-", "-"])

        # formateo
        widths = [len(h) for h in headers]
        for r in rows:
            for i, c in enumerate(r):
                widths[i] = max(widths[i], len(str(c)))

        def pad(s, w):
            s = str(s)
            return s + (" " * max(0, w - len(s)))

        lines = []
        lines.append(" | ".join(pad(headers[i], widths[i]) for i in range(len(headers))))
        lines.append("-+-".join("-" * widths[i] for i in range(len(headers))))

        if not rows:
            lines.append("(sin nodos aún)")
        else:
            for r in rows:
                lines.append(" | ".join(pad(r[i], widths[i]) for i in range(len(headers))))

        return "\n".join(lines)


def run_tracker_with_menu(bind_addr: str):
    service = TrackerService()
    service.load_state()

    # Arranca reloj de limpieza
    threading.Thread(target=service.cleanup_job, daemon=True).start()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=80))
    tracker_pb2_grpc.add_TrackerServicer_to_server(service, server)

    server.add_insecure_port(bind_addr)
    server.start()

    print(f"🚀 Tracker gRPC en {bind_addr}")
    print(f"📌 Persistencia: {STATE_FILE}")
    print("=== MENU TRACKER ===")
    print("1) Ver estado de los nodos")
    print("2) Listar torrents disponibles")
    print("3) Salir")

    while True:
        try:
            op = input("\nElige opción: ").strip()
        except (EOFError, KeyboardInterrupt):
            op = "3"

        if op == "1":
            print("\n" + service.build_nodes_table())
        elif op == "2":
            resp = service.ListTorrents(tracker_pb2.ListTorrentsRequest(), None)
            print("\n--- TORRENTS EN TRACKER ---")
            if not resp.torrents:
                print("(ninguno)")
            else:
                for t in resp.torrents:
                    print(f"- {t.torrent_name} | file={t.file_name} | id={t.info_hash[:8]}…")
        elif op == "3":
            print("Saliendo tracker...")
            server.stop(grace=0)
            return
        else:
            print("Opción inválida.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="0.0.0.0:50051", help="IP:PUERTO donde escucha el tracker")
    args = ap.parse_args()

    run_tracker_with_menu(args.bind)
