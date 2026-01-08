#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import time
import json
import threading
from pathlib import Path
from concurrent import futures

import grpc
import tracker_pb2, tracker_pb2_grpc

# ================= CONFIG =================

SERVABLE_THRESHOLD = 0.20
SEEDER_THRESHOLD = 0.999
HEARTBEAT_OFFLINE = 10      # segundos → marcar OFFLINE
HEARTBEAT_REMOVE  = 180    # segundos → eliminar de la red


STATE_FILE = Path.cwd() / "tracker_state.json"

# ================= UTIL =================
def atomic_write(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)

def role_from_percent(p):
    if p >= SEEDER_THRESHOLD:
        return "SEEDER"
    if p > 0:
        return "LEECHER"
    return "PEER"

# ================= STATE =================
class TrackerState:
    def __init__(self):
        self.lock = threading.RLock()
        self.peers = {}
        self.metas = {}
        self.load()

    def load(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self.peers = data.get("peers", {})
                self.metas = data.get("metas", {})
            except Exception:
                pass

    def save(self):
        atomic_write(STATE_FILE, {
            "peers": self.peers,
            "metas": self.metas,
            "saved_at": time.time()
        })

    def touch_peer(self, peer_uuid, ip, port):
        now = time.time()
        p = self.peers.get(peer_uuid)

        if not p:
            print(f"[CONNECT] {ip}:{port}")
            self.peers[peer_uuid] = {
                "ip": ip,
                "port": port,
                "last_seen": now,
                "online": True,
                "files": {}
            }
        else:
            if not p["online"]:
                print(f"[CONNECT] {ip}:{port}")
            p.update({
                "ip": ip,
                "port": port,
                "last_seen": now,
                "online": True
            })
        self.save()

    def mark_offline_peers(self):
        now = time.time()
        removed = False
        changed = False

        to_delete = []

        for peer_uuid, p in list(self.peers.items()):
            delta = now - p["last_seen"]

            # después de 10s -> OFFLINE
            if p["online"] and delta >= HEARTBEAT_OFFLINE:
                p["online"] = False
                print(f"[OFFLINE] {p['ip']}:{p['port']}")
                changed = True

            # después de 3 min -> eliminar
            if delta >= HEARTBEAT_REMOVE:
                print(f"[REMOVE] {p['ip']}:{p['port']} eliminado de la red")
                to_delete.append(peer_uuid)
                removed = True

        for peer_uuid in to_delete:
            del self.peers[peer_uuid]

        if changed or removed:
            self.save()


    def announce(self, peer_uuid, ip, port, files):
        self.touch_peer(peer_uuid, ip, port)
        peer = self.peers[peer_uuid]

        for f in files:
            peer["files"][f.infohash] = {
                "name": f.name,
                "total": f.total_pieces,
                "have": f.have_pieces,
                "percent": f.percent
            }

            if f.infohash not in self.metas and f.piece_length > 0:
                self.metas[f.infohash] = {
                    "name": f.name,
                    "size": f.size_bytes,
                    "piece_length": f.piece_length,
                    "total_pieces": f.total_pieces,
                    "pieces_blob": f.pieces_blob.hex()
                }

        print(f"[ANNOUNCE] {ip}:{port} actualizó {len(files)} archivo(s)")
        self.save()

    def list_files(self):
        self.mark_offline_peers()
        out = {}

        for p in self.peers.values():
            if not p["online"]:
                continue
            for ih, f in p["files"].items():
                if ih not in out:
                    out[ih] = {
                        "infohash": ih,
                        "name": f["name"],
                        "total": f["total"],
                        "servables": 0,
                        "seeders": 0
                    }
                if f["percent"] >= SERVABLE_THRESHOLD:
                    out[ih]["servables"] += 1
                if f["percent"] >= SEEDER_THRESHOLD:
                    out[ih]["seeders"] += 1
        return list(out.values())

    def get_peers_for_file(self, infohash):
        self.mark_offline_peers()
        peers = []
        for p in self.peers.values():
            if not p["online"]:
                continue
            f = p["files"].get(infohash)
            if f and f["percent"] >= SERVABLE_THRESHOLD:
                peers.append((p["ip"], p["port"]))
        return peers

    # ====== ESTADO DE LA RED (SWARM VIEW) ======
    def swarm_view(self):
        self.mark_offline_peers()
        now = time.time()

        lines = []
        lines.append("\n================ ESTADO DEL ENJAMBRE =================")

        for p in self.peers.values():
            age = int(now - p["last_seen"])
            status = "ONLINE" if p["online"] else "OFFLINE"
            lines.append(f"\nPEER {p['ip']}:{p['port']} | {status} | last_seen={age}s")

            if not p["files"]:
                lines.append("  (sin archivos anunciados)")
                continue

            for f in p["files"].values():
                role = role_from_percent(f["percent"])
                lines.append(
                    f"  - {f['name']} | "
                    f"{f['have']}/{f['total']} "
                    f"({f['percent']*100:.1f}%) | ROL={role}"
                )

        lines.append("\n======================================================\n")
        return "\n".join(lines)

# ================= GRPC =================
class TrackerServicer(tracker_pb2_grpc.TrackerServicer):
    def __init__(self, state):
        self.state = state

    def Announce(self, request, context):
        self.state.announce(
            request.peer.peer_uuid,
            request.peer.ip,
            request.peer.port,
            request.files
        )
        return tracker_pb2.AnnounceResponse(ok=True)

    def Heartbeat(self, request, context):
        self.state.touch_peer(
            request.peer.peer_uuid,
            request.peer.ip,
            request.peer.port
        )
        return tracker_pb2.HeartbeatResponse(ok=True)

    def UpdateProgress(self, request, context):
        self.state.announce(
            request.peer.peer_uuid,
            request.peer.ip,
            request.peer.port,
            [request.file]
        )
        return tracker_pb2.UpdateProgressResponse(ok=True)

    def ListFiles(self, request, context):
        files = self.state.list_files()
        resp = tracker_pb2.ListFilesResponse()
        for f in files:
            resp.files.append(tracker_pb2.FileEntry(
                infohash=f["infohash"],
                name=f["name"],
                total_pieces=f["total"],
                servables=f["servables"],
                seeders=f["seeders"]
            ))
        return resp

    def GetPeersForFile(self, request, context):
        peers = self.state.get_peers_for_file(request.infohash)
        resp = tracker_pb2.GetPeersForFileResponse()
        for ip, port in peers:
            resp.peers.append(tracker_pb2.PeerId(ip=ip, port=port))
        return resp

    def GetMeta(self, request, context):
        meta = self.state.metas.get(request.infohash)
        if not meta:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return tracker_pb2.GetMetaResponse()

        return tracker_pb2.GetMetaResponse(
            infohash=request.infohash,
            name=meta["name"],
            size_bytes=meta["size"],
            piece_length=meta["piece_length"],
            total_pieces=meta["total_pieces"],
            pieces_blob=bytes.fromhex(meta["pieces_blob"])
        )

# ================= MENU =================
def tracker_menu(state: TrackerState):
    print("\n[TRACKER] Menú:")
    print("  1) Ver estado del enjambre")
    print("  q) Salir\n")

    while True:
        cmd = input("tracker> ").strip().lower()
        if cmd == "1":
            print(state.swarm_view())
        elif cmd == "q":
            break
        elif cmd == "":
            continue
        else:
            print("Opción inválida.\n")

# ================= MAIN =================
def main():
    if len(sys.argv) != 3:
        print("Uso: python3 tracker_server.py <IP> <PUERTO>")
        sys.exit(1)

    ip = sys.argv[1]
    port = int(sys.argv[2])

    state = TrackerState()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=50))
    tracker_pb2_grpc.add_TrackerServicer_to_server(TrackerServicer(state), server)
    server.add_insecure_port(f"{ip}:{port}")
    server.start()

    print(f"[TRACKER] Corriendo en {ip}:{port}")
    print("[TRACKER] Esperando peers...\n")

    try:
        tracker_menu(state)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[TRACKER] Apagado.")
        server.stop(1)

if __name__ == "__main__":
    main()
