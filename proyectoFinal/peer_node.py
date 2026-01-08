#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import time
import json
import threading
import hashlib
import socket
import uuid
from pathlib import Path
from concurrent import futures

import grpc

import tracker_pb2, tracker_pb2_grpc
import peer_pb2, peer_pb2_grpc

from torrent_utils import read_torrent, piece_sha1_expected

# --- paths ---
PROJECT_ROOT = Path.cwd()
DOWNLOADS_DIR = PROJECT_ROOT / "Descargas"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

LOCAL_TORRENT_DIR = (Path.home() / "Escritorio" / "archivos" / "Torrent")
if not LOCAL_TORRENT_DIR.exists():
    LOCAL_TORRENT_DIR = (Path.home() / "Desktop" / "archivos" / "Torrent")

LOCAL_MEDIA_DIR = (Path.home() / "Escritorio" / "archivos" / "Multimedia")
if not LOCAL_MEDIA_DIR.exists():
    LOCAL_MEDIA_DIR = (Path.home() / "Desktop" / "archivos" / "Multimedia")

STATE_FILE = PROJECT_ROOT / "peer_state.json"
PEER_ID_FILE = PROJECT_ROOT / "peer_id.json"

HEARTBEAT_SECONDS = 10
ANNOUNCE_SECONDS  = 15
SERVABLE_THRESHOLD = 0.20


def detect_local_ip(tracker_ip: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((tracker_ip, 1))
        return s.getsockname()[0]
    finally:
        s.close()


def load_or_create_peer_uuid() -> str:
    try:
        if PEER_ID_FILE.exists():
            data = json.loads(PEER_ID_FILE.read_text(encoding="utf-8"))
            puid = (data.get("peer_uuid") or "").strip()
            if puid:
                return puid
    except Exception:
        pass

    puid = str(uuid.uuid4())
    PEER_ID_FILE.write_text(json.dumps({"peer_uuid": puid}, indent=2), encoding="utf-8")
    return puid


class PeerState:
    # torrents[infohash] = { meta..., have: set(piece_index), path: ... }
    def __init__(self):
        self.lock = threading.RLock()
        self.torrents = {}

    def load(self):
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            with self.lock:
                self.torrents = {}
                for ih, t in data.get("torrents", {}).items():
                    self.torrents[ih] = {
                        "infohash": ih,
                        "name": t["name"],
                        "length": t["length"],
                        "piece_length": t["piece_length"],
                        "total_pieces": t["total_pieces"],
                        "pieces_blob_hex": t["pieces_blob_hex"],
                        "path": t["path"],
                        "have": set(t.get("have", [])),
                    }
        except Exception:
            pass

    def save(self):
        with self.lock:
            out = {"torrents": {}}
            for ih, t in self.torrents.items():
                out["torrents"][ih] = {
                    "name": t["name"],
                    "length": t["length"],
                    "piece_length": t["piece_length"],
                    "total_pieces": t["total_pieces"],
                    "pieces_blob_hex": t["pieces_blob_hex"],
                    "path": t["path"],
                    "have": sorted(list(t["have"])),
                }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)

    def add_or_update_torrent(self, meta: dict, file_path: Path, have_pieces: set):
        with self.lock:
            self.torrents[meta["infohash"]] = {
                "infohash": meta["infohash"],
                "name": meta["name"],
                "length": meta["length"],
                "piece_length": meta["piece_length"],
                "total_pieces": meta["total_pieces"],
                "pieces_blob_hex": meta["pieces_blob"].hex(),
                "path": str(file_path),
                "have": set(have_pieces),
            }
        self.save()

    def get_progress(self, ih: str):
        with self.lock:
            t = self.torrents.get(ih)
            if not t:
                return 0, 0, 0.0
            have = len(t["have"])
            total = t["total_pieces"]
            pct = (have / total) if total else 0.0
            return have, total, pct

    def list_torrents(self):
        with self.lock:
            return list(self.torrents.values())

    def get_torrent(self, ih: str):
        with self.lock:
            return self.torrents.get(ih)

    def mark_have(self, ih: str, piece_index: int):
        with self.lock:
            t = self.torrents[ih]
            t["have"].add(piece_index)
        self.save()


class PeerFileServicer(peer_pb2_grpc.PeerFileServiceServicer):
    def __init__(self, state: PeerState):
        self.state = state

    def GetFileStatus(self, request, context):
        t = self.state.get_torrent(request.infohash)
        if not t:
            return peer_pb2.GetFileStatusResponse(infohash=request.infohash, total_pieces=0)

        have_list = sorted(list(t["have"]))
        return peer_pb2.GetFileStatusResponse(
            infohash=request.infohash,
            total_pieces=t["total_pieces"],
            have_piece_indices=have_list
        )

    def DownloadPiece(self, request, context):
        t = self.state.get_torrent(request.infohash)
        if not t:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("No tengo ese archivo")
            return peer_pb2.DownloadPieceResponse()

        idx = int(request.piece_index)
        if idx not in t["have"]:
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("No tengo esa pieza")
            return peer_pb2.DownloadPieceResponse()

        file_path = Path(t["path"])
        piece_len = int(t["piece_length"])
        offset = idx * piece_len
        size = int(t["length"])
        read_len = min(piece_len, max(0, size - offset))

        with file_path.open("rb") as f:
            f.seek(offset)
            data = f.read(read_len)

        return peer_pb2.DownloadPieceResponse(data=data)


class PeerNode:
    def __init__(self, tracker_ip: str, tracker_port: int, peer_port: int):
        self.tracker_ip = tracker_ip
        self.tracker_port = tracker_port
        self.peer_port = peer_port

        self.peer_uuid = load_or_create_peer_uuid()
        self.ip = detect_local_ip(tracker_ip)

        self.state = PeerState()
        self.state.load()

        self._stop = threading.Event()

        self.tracker_channel = grpc.insecure_channel(f"{tracker_ip}:{tracker_port}")
        self.tracker = tracker_pb2_grpc.TrackerStub(self.tracker_channel)

    def start_server(self):
        self.grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
        peer_pb2_grpc.add_PeerFileServiceServicer_to_server(PeerFileServicer(self.state), self.grpc_server)
        self.grpc_server.add_insecure_port(f"0.0.0.0:{self.peer_port}")
        self.grpc_server.start()
        print(f"[PEER] UUID: {self.peer_uuid}")
        print(f"[PEER] Server listo en {self.ip}:{self.peer_port}")

    def stop(self):
        self._stop.set()
        try:
            self.grpc_server.stop(grace=1)
        except Exception:
            pass

    def _peer_id(self):
        return tracker_pb2.PeerId(peer_uuid=self.peer_uuid, ip=self.ip, port=self.peer_port)

    def _announce_payload(self):
        files = []
        for t in self.state.list_torrents():
            have, total, pct = self.state.get_progress(t["infohash"])

            try:
                pieces_blob = bytes.fromhex(t.get("pieces_blob_hex", "") or "")
            except Exception:
                pieces_blob = b""

            files.append(tracker_pb2.FileProgress(
                infohash=t["infohash"],
                name=t["name"],
                size_bytes=t["length"],
                total_pieces=t["total_pieces"],
                have_pieces=have,
                percent=pct,

                # metainfo para GetMeta
                piece_length=int(t["piece_length"]),
                pieces_blob=pieces_blob,
            ))
        return tracker_pb2.AnnounceRequest(peer=self._peer_id(), files=files)

    def background_announce_and_heartbeat(self):
        def loop():
            last_announce = 0.0
            while not self._stop.is_set():
                try:
                    self.tracker.Heartbeat(tracker_pb2.HeartbeatRequest(peer=self._peer_id()))
                except Exception:
                    pass

                now = time.time()
                if now - last_announce >= ANNOUNCE_SECONDS:
                    try:
                        self.tracker.Announce(self._announce_payload())
                    except Exception:
                        pass
                    last_announce = now

                time.sleep(HEARTBEAT_SECONDS)

        threading.Thread(target=loop, daemon=True).start()

    # -------- MENU ACTIONS --------
    def menu_1_download_local(self):
        if not LOCAL_TORRENT_DIR.exists():
            print('Lo siento, no existe ~/Escritorio/archivos/Torrent. Debe descargar contenido por la red.\n')
            return

        torrents = sorted(LOCAL_TORRENT_DIR.glob("*.torrent"))
        if not torrents:
            print("No hay .torrent en ~/Escritorio/archivos/Torrent\n")
            return

        print("\nTorrents locales:")
        for i, p in enumerate(torrents, 1):
            print(f" [{i}] {p.name}")
        choice = input("Elige número (o enter para cancelar): ").strip()
        if not choice.isdigit():
            print()
            return
        idx = int(choice)
        if idx < 1 or idx > len(torrents):
            print()
            return

        tpath = torrents[idx-1]
        meta = read_torrent(tpath)

        candidates = [
            LOCAL_MEDIA_DIR / meta["name"],
            Path.home() / "Escritorio" / "archivos" / meta["name"],
        ]
        src = None
        for c in candidates:
            if c.exists() and c.is_file():
                src = c
                break

        if src is None:
            print("Encontré el .torrent pero NO el archivo fuente en ~/Escritorio/archivos. Debe descargar por la red.\n")
            return

        dst = DOWNLOADS_DIR / meta["name"]
        dst.write_bytes(src.read_bytes())

        have = set(range(meta["total_pieces"]))
        self.state.add_or_update_torrent(meta, dst, have)

        try:
            self.tracker.Announce(self._announce_payload())
        except Exception:
            pass

        print(f"✅ Importado localmente: {meta['name']} -> {dst}\n")

    def _get_meta_from_tracker(self, infohash: str):
        try:
            meta_resp = self.tracker.GetMeta(tracker_pb2.GetMetaRequest(infohash=infohash))
        except Exception as e:
            print(f"No pude obtener metainfo (GetMeta) del tracker: {e}\n")
            return None

        if not meta_resp or not meta_resp.name or meta_resp.total_pieces <= 0:
            print("El tracker no tiene metainfo válida de ese archivo.\n")
            return None

        meta = {
            "infohash": infohash,
            "name": meta_resp.name,
            "length": int(meta_resp.size_bytes),
            "piece_length": int(meta_resp.piece_length),
            "total_pieces": int(meta_resp.total_pieces),
            "pieces_blob": bytes(meta_resp.pieces_blob),
        }

        expected_len = 20 * meta["total_pieces"]
        if len(meta["pieces_blob"]) != expected_len:
            print(f"Metainfo inconsistente: pieces_blob={len(meta['pieces_blob'])} pero esperaba {expected_len}.\n")
            return None

        return meta

    def menu_2_download_from_network(self):
        try:
            resp = self.tracker.ListFiles(tracker_pb2.ListFilesRequest())
        except Exception as e:
            print(f"No pude consultar tracker: {e}\n")
            return

        if not resp.files:
            print("No hay archivos disponibles en la red.\n")
            return

        print("\nArchivos disponibles en la red (>=20% para servir):")
        files = list(resp.files)
        for i, f in enumerate(files, 1):
            print(f" [{i}] {f.name} | size={f.size_bytes} | pieces={f.total_pieces} | servables={f.servables} | seeders={f.seeders}")
        choice = input("Elige número (o enter para cancelar): ").strip()
        if not choice.isdigit():
            print()
            return
        idx = int(choice)
        if idx < 1 or idx > len(files):
            print()
            return

        chosen = files[idx-1]
        infohash = chosen.infohash

        # peers servables
        try:
            peers_resp = self.tracker.GetPeersForFile(tracker_pb2.GetPeersForFileRequest(infohash=infohash))
        except Exception as e:
            print(f"No pude obtener peers: {e}\n")
            return

        peers = [(p.peer_uuid, p.ip, p.port) for p in peers_resp.peers
                 if not (p.ip == self.ip and p.port == self.peer_port)]
        if not peers:
            print("No hay peers disponibles (distintos a mí) que puedan servir ese archivo.\n")
            return

        # metainfo desde tracker (NO torrent)
        meta = self._get_meta_from_tracker(infohash)
        if not meta:
            return

        dst = DOWNLOADS_DIR / meta["name"]
        part = DOWNLOADS_DIR / (meta["name"] + ".part")

        if dst.exists():
            print("Ya existe el archivo final en Descargas. Si quieres re-descargar, bórralo primero.\n")
            return

        # -------- RESUME REAL --------
        existing = self.state.get_torrent(infohash)
        if existing:
            # si ya lo tenías, recupera have
            have_set = set(existing.get("have", set()))
            # y asegúrate que apunta al .part
            existing_path = Path(existing.get("path", str(part)))
            if existing_path.exists():
                part = existing_path
        else:
            have_set = set()

        # si no existe .part, crearlo (una sola vez)
        if not part.exists():
            with part.open("wb") as f:
                f.truncate(meta["length"])
            print(f"[RESUME] Creado archivo parcial: {part.name}")
        else:
            # si existe, validar tamaño mínimo
            try:
                if part.stat().st_size != meta["length"]:
                    # Si el tamaño no coincide, lo corregimos (truncate)
                    with part.open("r+b") as f:
                        f.truncate(meta["length"])
                print(f"[RESUME] Detectado parcial existente: {part.name}")
            except Exception:
                pass

        # guarda/actualiza state con path .part y have actual
        self.state.add_or_update_torrent(meta, part, have_set)

        total = meta["total_pieces"]
        piece_len = meta["piece_length"]
        pieces_blob = meta["pieces_blob"]

        # Obtener disponibilidad de piezas por peer
        peer_status = []
        for puid, ip, port in peers:
            try:
                ch = grpc.insecure_channel(f"{ip}:{port}")
                stub = peer_pb2_grpc.PeerFileServiceStub(ch)
                st = stub.GetFileStatus(peer_pb2.GetFileStatusRequest(infohash=infohash), timeout=3)
                peer_status.append((puid, ip, port, set(st.have_piece_indices)))
            except Exception:
                continue

        if not peer_status:
            print("No pude obtener estatus de piezas de ningún peer.\n")
            return

        have_now, total_now, pct_now = self.state.get_progress(infohash)
        missing = total - have_now
        print(f"\nDescargando {meta['name']} ({total} piezas)")
        print(f"[RESUME] Progreso inicial: {have_now}/{total_now} ({pct_now*100:.1f}%) | faltan {missing} piezas\n")

        # Descargar SOLO piezas faltantes
        for piece_index in range(total):
            # saltar si ya la tienes
            tcur = self.state.get_torrent(infohash)
            if tcur and piece_index in tcur["have"]:
                continue

            # elige un peer que la tenga
            chosen_peer = None
            for puid, ip, port, have_set_peer in peer_status:
                if piece_index in have_set_peer:
                    chosen_peer = (ip, port)
                    break
            if chosen_peer is None:
                chosen_peer = (peer_status[0][1], peer_status[0][2])

            ip, port = chosen_peer

            try:
                ch = grpc.insecure_channel(f"{ip}:{port}")
                stub = peer_pb2_grpc.PeerFileServiceStub(ch)
                resp_piece = stub.DownloadPiece(peer_pb2.DownloadPieceRequest(infohash=infohash, piece_index=piece_index), timeout=10)
                data = resp_piece.data

                expected = piece_sha1_expected(pieces_blob, piece_index)
                got = hashlib.sha1(data).digest()

                if got != expected:
                    print(f"❌ Pieza {piece_index} inválida (hash mismatch). Reintentando...")
                    ok = False
                    for _, ip2, port2, have2 in peer_status:
                        if ip2 == ip and port2 == port:
                            continue
                        if piece_index not in have2:
                            continue
                        try:
                            ch2 = grpc.insecure_channel(f"{ip2}:{port2}")
                            stub2 = peer_pb2_grpc.PeerFileServiceStub(ch2)
                            r2 = stub2.DownloadPiece(peer_pb2.DownloadPieceRequest(infohash=infohash, piece_index=piece_index), timeout=10)
                            data2 = r2.data
                            if hashlib.sha1(data2).digest() == expected:
                                data = data2
                                ok = True
                                break
                        except Exception:
                            continue
                    if not ok:
                        print(f"❌ No pude descargar una pieza válida {piece_index}. Abortando.\n")
                        return

                # escribir en offset
                offset = piece_index * piece_len
                with part.open("r+b") as f:
                    f.seek(offset)
                    f.write(data)

                self.state.mark_have(infohash, piece_index)

                have_now, total_now, pct = self.state.get_progress(infohash)
                try:
                    self.tracker.UpdateProgress(tracker_pb2.UpdateProgressRequest(
                        peer=self._peer_id(),
                        file=tracker_pb2.FileProgress(
                            infohash=infohash,
                            name=meta["name"],
                            size_bytes=meta["length"],
                            total_pieces=meta["total_pieces"],
                            have_pieces=have_now,
                            percent=pct,
                            piece_length=meta["piece_length"],
                            pieces_blob=meta["pieces_blob"],
                        )
                    ))
                except Exception:
                    pass

                if have_now % 10 == 0 or have_now == total:
                    print(f"  progreso: {have_now}/{total_now} ({pct*100:.1f}%)")

            except Exception as e:
                print(f"Error descargando pieza {piece_index} desde {ip}:{port}: {e}\n")
                return

        # Completo -> renombrar
        part.replace(dst)

        # Actualiza state a final con todas las piezas
        have_all = set(range(total))
        self.state.add_or_update_torrent(meta, dst, have_all)

        try:
            self.tracker.Announce(self._announce_payload())
        except Exception:
            pass

        print(f"\n✅ Descarga completa: {dst}\n")

    def menu_3_view_progress(self):
        torrents = self.state.list_torrents()
        if not torrents:
            print("No tienes torrents en Descargas todavía.\n")
            return
        print("\nContenidos y progreso:")
        for t in torrents:
            have, total, pct = self.state.get_progress(t["infohash"])
            path = t.get("path", "")
            print(f" - {t['name']}: {have}/{total} piezas ({pct*100:.1f}%) | {path}")
        print()

    def run_menu(self):
        print(f"\n[PEER] Mi identidad: {self.ip}:{self.peer_port}")
        print(f"[PEER] UUID estable: {self.peer_uuid}")
        print("[PEER] Opciones:")
        print("  1. Descargar archivo localmente (solo peer inicial)")
        print("  2. Descargar archivo de la red (con RESUME)")
        print("  3. Ver contenidos y progreso")
        print("  q. Salir\n")

        while True:
            cmd = input("> ").strip().lower()
            if cmd == "1":
                self.menu_1_download_local()
            elif cmd == "2":
                self.menu_2_download_from_network()
            elif cmd == "3":
                self.menu_3_view_progress()
            elif cmd == "q":
                break
            elif cmd == "":
                continue
            else:
                print("Opción inválida.\n")


def main():
    if len(sys.argv) != 4:
        print("Uso: python3 peer_node.py <IP_TRACKER> <PUERTO_TRACKER> <PUERTO_PEER>")
        sys.exit(1)

    tracker_ip = sys.argv[1].strip()
    tracker_port = int(sys.argv[2].strip())
    peer_port = int(sys.argv[3].strip())

    node = PeerNode(tracker_ip, tracker_port, peer_port)
    node.start_server()
    node.background_announce_and_heartbeat()

    try:
        node.run_menu()
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()


if __name__ == "__main__":
    main()
