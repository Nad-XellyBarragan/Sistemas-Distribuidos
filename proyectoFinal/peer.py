import os
import json
import time
import uuid
import hashlib
import threading
import argparse
from concurrent import futures
from dataclasses import dataclass
from typing import List, Optional, Set, Dict

import grpc

import tracker_pb2
import tracker_pb2_grpc
import peer_pb2
import peer_pb2_grpc


PEDAZO_TAM = 102400
STREAM_BLOCK = 64 * 1024

HEARTBEAT_EVERY_SEC = 5   # heartbeat global al tracker
TORRENT_ANNOUNCE_HEARTBEAT_SEC = 5  # actualiza progreso/online por torrent (silencioso)


def md5_hex(data: bytes) -> str:
    h = hashlib.md5()
    h.update(data)
    return h.hexdigest()


@dataclass
class TorrentMeta:
    torrent_id: str
    tracker_ip: str
    tracker_port: int
    pieces: int
    last_piece: int
    name: str
    filepath: str
    checksum: List[str]
    torrent_filename: str  # ej: video.torrent.json

    @property
    def tracker_addr(self) -> str:
        return f"{self.tracker_ip}:{self.tracker_port}"

    @property
    def total_size(self) -> int:
        if self.pieces <= 0:
            return 0
        return (self.pieces - 1) * PEDAZO_TAM + self.last_piece


class TorrentState:
    """
    Estado local de 1 torrent.
    Seeder: sirve piezas leyendo el archivo real (filepath) si existe y tamaño cuadra.
    Leecher: guarda piezas en downloads/<id>/pieces/piece_i.bin
    """
    def __init__(self, meta: TorrentMeta, base_dir: str, downloads_dir: str):
        self.meta = meta
        self.base_dir = base_dir
        self.downloads_dir = downloads_dir

        self._lock = threading.Lock()
        self._have: Set[int] = set()

        self.torrent_dir = os.path.join(self.downloads_dir, self.meta.torrent_id)
        self.pieces_dir = os.path.join(self.torrent_dir, "pieces")
        os.makedirs(self.pieces_dir, exist_ok=True)

        self.out_file_path = os.path.join(self.torrent_dir, self.meta.name)
        self.seed_file_path = os.path.join(self.base_dir, self.meta.filepath)

        self._load_existing_pieces_from_disk()

    def _piece_path(self, idx: int) -> str:
        return os.path.join(self.pieces_dir, f"piece_{idx}.bin")

    def _expected_piece_size(self, idx: int) -> int:
        return PEDAZO_TAM if idx < self.meta.pieces - 1 else self.meta.last_piece

    def _load_existing_pieces_from_disk(self):
        for idx in range(self.meta.pieces):
            if os.path.isfile(self._piece_path(idx)):
                self._have.add(idx)

    def is_seed_file_available(self) -> bool:
        return os.path.isfile(self.seed_file_path) and os.path.getsize(self.seed_file_path) == self.meta.total_size

    def is_complete(self) -> bool:
        with self._lock:
            return len(self._have) == self.meta.pieces

    def have_count(self) -> int:
        with self._lock:
            return len(self._have)

    def bytes_downloaded(self) -> int:
        total = 0
        with self._lock:
            have = list(self._have)
        for idx in have:
            p = self._piece_path(idx)
            if os.path.isfile(p):
                total += os.path.getsize(p)
        return total

    def status(self) -> str:
        return "seeder" if (self.is_seed_file_available() or self.is_complete()) else "leecher"

    def list_have(self) -> List[int]:
        with self._lock:
            return sorted(self._have)

    def validate_piece(self, idx: int, data: bytes) -> bool:
        if idx < 0 or idx >= self.meta.pieces:
            return False
        if len(data) != self._expected_piece_size(idx):
            return False
        return md5_hex(data) == self.meta.checksum[idx]

    def save_piece(self, idx: int, data: bytes):
        if not self.validate_piece(idx, data):
            raise ValueError(f"Pieza {idx} inválida (tamaño/hash)")
        path = self._piece_path(idx)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        with self._lock:
            self._have.add(idx)

    def get_piece_bytes(self, idx: int) -> Optional[bytes]:
        if idx < 0 or idx >= self.meta.pieces:
            return None

        # 1) si tengo archivo seed completo, lo corto
        if self.is_seed_file_available():
            start = idx * PEDAZO_TAM
            size = self._expected_piece_size(idx)
            try:
                with open(self.seed_file_path, "rb") as f:
                    f.seek(start)
                    data = f.read(size)
                return data if self.validate_piece(idx, data) else None
            except OSError:
                return None

        # 2) si no, pieza descargada
        p = self._piece_path(idx)
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "rb") as f:
                data = f.read()
            return data if self.validate_piece(idx, data) else None
        except OSError:
            return None

    def assemble_if_complete(self) -> Optional[str]:
        if not self.is_complete():
            return None

        if os.path.isfile(self.out_file_path) and os.path.getsize(self.out_file_path) == self.meta.total_size:
            return self.out_file_path

        with open(self.out_file_path, "wb") as out:
            for idx in range(self.meta.pieces):
                data = self.get_piece_bytes(idx)
                if data is None:
                    return None
                out.write(data)

        if os.path.getsize(self.out_file_path) != self.meta.total_size:
            return None
        return self.out_file_path


class PeerP2PService(peer_pb2_grpc.PeerServicer):
    def __init__(self, state_getter):
        self.state_getter = state_getter

    def GetHavePieces(self, request, context):
        st = self.state_getter(request.info_hash)
        if not st:
            return peer_pb2.GetHavePiecesResponse(have=[])
        return peer_pb2.GetHavePiecesResponse(have=st.list_have())

    def GetPiece(self, request, context):
        st = self.state_getter(request.info_hash)
        if not st:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("torrent_id no disponible")
            return

        idx = request.piece_index
        data = st.get_piece_bytes(idx)
        if data is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("pieza no disponible en este peer")
            return

        off = 0
        while off < len(data):
            yield peer_pb2.PieceData(data=data[off:off + STREAM_BLOCK])
            off += STREAM_BLOCK


class PeerApp:
    def __init__(self, bind_addr: str, base_dir: str, tracker_addr: str):
        self.bind_addr = bind_addr
        self.base_dir = base_dir
        self.tracker_addr = tracker_addr  # "ip:50051"

        self.peer_id = f"peer-{uuid.uuid4().hex[:6]}"

        self.downloads_dir = os.path.join(self.base_dir, "downloads")
        os.makedirs(self.downloads_dir, exist_ok=True)

        self._states_lock = threading.Lock()
        self._states: Dict[str, TorrentState] = {}

        self._stop_download = threading.Event()
        self._download_threads: Dict[str, threading.Thread] = {}

        self._stop_global_hb = threading.Event()
        self._global_hb_thread: Optional[threading.Thread] = None

        self._stop_torrent_hb = threading.Event()
        self._torrent_hb_threads: Dict[str, threading.Thread] = {}

        self._start_server()

        # ✅ 1) Voceo global (aunque no haya torrents)
        self._hello_tracker()
        self._start_global_heartbeat()

        # ✅ 2) Si tengo torrents locales: publicar al tracker + announce (SIN descargar)
        self.publish_and_announce_local_torrents()

    # ---------- bind helpers ----------
    def _bind_ip(self) -> str:
        return self.bind_addr.split(":")[0]

    def _bind_port(self) -> int:
        return int(self.bind_addr.split(":")[1])

    # ---------- grpc stubs ----------
    def _tracker_stub(self):
        ch = grpc.insecure_channel(self.tracker_addr)
        return ch, tracker_pb2_grpc.TrackerStub(ch)

    # ---------- state ----------
    def get_state_by_hash(self, info_hash: str) -> Optional[TorrentState]:
        with self._states_lock:
            return self._states.get(info_hash)

    def set_state(self, st: TorrentState):
        with self._states_lock:
            self._states[st.meta.torrent_id] = st

    def list_loaded_states(self) -> List[TorrentState]:
        with self._states_lock:
            return list(self._states.values())

    # ---------- tracker: hello / heartbeat ----------
    def _hello_tracker(self):
        ch, stub = self._tracker_stub()
        try:
            stub.Hello(tracker_pb2.HelloRequest(
                peer=tracker_pb2.PeerInfo(peer_id=self.peer_id, ip=self._bind_ip(), port=self._bind_port())
            ))
        finally:
            ch.close()

    def _start_global_heartbeat(self):
        if self._global_hb_thread and self._global_hb_thread.is_alive():
            return

        def loop():
            while not self._stop_global_hb.is_set():
                try:
                    ch, stub = self._tracker_stub()
                    try:
                        stub.Heartbeat(tracker_pb2.HeartbeatRequest(peer_id=self.peer_id))
                    finally:
                        ch.close()
                except Exception:
                    pass
                time.sleep(HEARTBEAT_EVERY_SEC)

        self._global_hb_thread = threading.Thread(target=loop, daemon=True)
        self._global_hb_thread.start()

    # ---------- server ----------
    def _start_server(self):
        def serve():
            server = grpc.server(futures.ThreadPoolExecutor(max_workers=80))
            peer_pb2_grpc.add_PeerServicer_to_server(PeerP2PService(self.get_state_by_hash), server)
            server.add_insecure_port(self.bind_addr)
            server.start()
            print(f"🟢 Peer gRPC arriba en {self.bind_addr} (peer_id={self.peer_id})")
            server.wait_for_termination()

        threading.Thread(target=serve, daemon=True).start()

    # ---------- torrents scan/load ----------
    def _torrents_dir(self) -> str:
        return os.path.join(self.base_dir, "torrent")

    def _list_torrents(self) -> List[str]:
        td = self._torrents_dir()
        if not os.path.isdir(td):
            return []
        files = [f for f in os.listdir(td) if f.endswith(".torrent.json") or f.endswith(".torrent")]
        files.sort()
        return [os.path.join(td, f) for f in files]

    def _load_torrent_file(self, path: str) -> TorrentMeta:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)

        required = ["id", "tracker", "puertoTracker", "pieces", "lastPiece", "name", "filepath", "checksum"]
        for k in required:
            if k not in obj:
                raise ValueError(f"Torrent inválido, falta campo: {k}")

        return TorrentMeta(
            torrent_id=str(obj["id"]),
            tracker_ip=str(obj["tracker"]),
            tracker_port=int(obj["puertoTracker"]),
            pieces=int(obj["pieces"]),
            last_piece=int(obj["lastPiece"]),
            name=str(obj["name"]),
            filepath=str(obj["filepath"]),
            checksum=list(obj["checksum"]),
            torrent_filename=os.path.basename(path),
        )

    # ---------- NEW: publish & announce local torrents ----------
    def publish_and_announce_local_torrents(self):
        torrents = self._list_torrents()
        if not torrents:
            print("ℹ️ No hay torrents locales en torrent/. Este peer igual quedó registrado (Hello).")
            return

        print(f"📣 Torrents locales detectados: {len(torrents)}. Publicando al tracker (sin descargar).")
        for path in torrents:
            try:
                meta = self._load_torrent_file(path)
                st = TorrentState(meta, base_dir=self.base_dir, downloads_dir=self.downloads_dir)
                self.set_state(st)

                # 1) Publicar el torrent JSON al tracker (catálogo)
                with open(path, "rb") as f:
                    raw = f.read()

                ch, stub = self._tracker_stub()
                try:
                    stub.PublishTorrent(tracker_pb2.PublishTorrentRequest(
                        info_hash=meta.torrent_id,
                        meta=tracker_pb2.TorrentEntry(
                            info_hash=meta.torrent_id,
                            torrent_name=meta.torrent_filename,
                            file_name=meta.name,
                            pieces=meta.pieces,
                            last_piece=meta.last_piece,
                        ),
                        torrent_json=raw
                    ))
                finally:
                    ch.close()

                # 2) Announce por torrent (para que tracker sepa que soy seeder/leecher de ese torrent)
                self.announce(st)

                # 3) Heartbeat por torrent (UpdateProgress silencioso para online/offline + progreso cuando cambie)
                self._start_torrent_heartbeat(st)

                role = "SEEDER" if st.status() == "seeder" else "LEECHER"
                print(f"  - publicado/announce: {meta.torrent_filename} rol={role}")

            except Exception as e:
                print(f"⚠️ Error publicando {os.path.basename(path)}: {e}")

    # ---------- existing: announce/progress/peers/progress ----------
    def announce(self, st: TorrentState):
        ch, stub = self._tracker_stub()
        try:
            stub.Announce(tracker_pb2.AnnounceRequest(
                info_hash=st.meta.torrent_id,
                peer=tracker_pb2.PeerInfo(peer_id=self.peer_id, ip=self._bind_ip(), port=self._bind_port()),
                is_seeder=(st.status() == "seeder"),
                total_pieces=st.meta.pieces,
                torrent_name=st.meta.torrent_filename,
            ))
        finally:
            ch.close()

    def update_progress(self, st: TorrentState):
        ch, stub = self._tracker_stub()
        try:
            pieces_have = st.have_count() if not st.is_seed_file_available() else st.meta.pieces
            bytes_done = st.bytes_downloaded() if not st.is_seed_file_available() else st.meta.total_size
            stub.UpdateProgress(tracker_pb2.UpdateProgressRequest(
                info_hash=st.meta.torrent_id,
                peer_id=self.peer_id,
                pieces_have=pieces_have,
                bytes_downloaded=bytes_done,
                status=st.status()
            ))
        finally:
            ch.close()

    def get_peers(self, st: TorrentState):
        ch, stub = self._tracker_stub()
        try:
            return stub.GetPeers(tracker_pb2.GetPeersRequest(info_hash=st.meta.torrent_id)).peers
        finally:
            ch.close()

    def get_progress(self, st: TorrentState):
        ch, stub = self._tracker_stub()
        try:
            return stub.GetProgress(tracker_pb2.GetProgressRequest(info_hash=st.meta.torrent_id)).progress
        finally:
            ch.close()

    # ---------- torrent heartbeat ----------
    def _start_torrent_heartbeat(self, st: TorrentState):
        ih = st.meta.torrent_id
        if ih in self._torrent_hb_threads and self._torrent_hb_threads[ih].is_alive():
            return

        def loop():
            while not self._stop_torrent_hb.is_set():
                try:
                    self.update_progress(st)  # tracker solo imprime si hay cambio real
                except Exception:
                    pass
                time.sleep(TORRENT_ANNOUNCE_HEARTBEAT_SEC)

        t = threading.Thread(target=loop, daemon=True)
        self._torrent_hb_threads[ih] = t
        t.start()

    # ---------- NEW: add torrent from tracker ----------
    def menu_add_torrent_from_tracker(self):
        ch, stub = self._tracker_stub()
        try:
            resp = stub.ListTorrents(tracker_pb2.ListTorrentsRequest())
        finally:
            ch.close()

        if not resp.torrents:
            print("\n❌ No hay torrents publicados en el tracker.")
            return

        print("\n=== Torrents disponibles en Tracker ===")
        for i, t in enumerate(resp.torrents, start=1):
            print(f"{i}. {t.torrent_name}  | file={t.file_name} | pieces={t.pieces}")

        sel = input("Elige número para descargar el .torrent.json: ").strip()
        if not sel.isdigit():
            print("Opción inválida.")
            return
        idx = int(sel)
        if idx < 1 or idx > len(resp.torrents):
            print("Fuera de rango.")
            return

        chosen = resp.torrents[idx - 1]

        ch, stub = self._tracker_stub()
        try:
            got = stub.GetTorrent(tracker_pb2.GetTorrentRequest(info_hash=chosen.info_hash))
        finally:
            ch.close()

        if not got.torrent_json:
            print("❌ El tracker no devolvió el torrent.")
            return

        os.makedirs(self._torrents_dir(), exist_ok=True)
        out_path = os.path.join(self._torrents_dir(), chosen.torrent_name or f"{chosen.info_hash}.torrent.json")
        with open(out_path, "wb") as f:
            f.write(got.torrent_json)

        print(f"✅ Torrent guardado en: {out_path}")

        # cargarlo en memoria y anunciar/heartbeat por torrent (sin descargar)
        meta = self._load_torrent_file(out_path)
        st = TorrentState(meta, base_dir=self.base_dir, downloads_dir=self.downloads_dir)
        self.set_state(st)
        self.announce(st)
        self._start_torrent_heartbeat(st)
        print("📣 Anunciado al tracker (sin descargar).")

    # ---------- download ----------
    def _start_download(self, st: TorrentState):
        ih = st.meta.torrent_id
        if ih in self._download_threads and self._download_threads[ih].is_alive():
            print("🧵 Descarga ya está corriendo para este torrent.")
            return

        def loop():
            while not self._stop_download.is_set():
                if st.is_complete():
                    out = st.assemble_if_complete()
                    if out:
                        print(f"✅ Descarga completa ({st.meta.torrent_filename}). Archivo armado en: {out}")
                    # al completar cambia a seeder (update_progress lo reflejará)
                    return

                peers = self.get_peers(st)
                peers = [p for p in peers if p.peer_id != self.peer_id]
                if not peers:
                    time.sleep(2)
                    continue

                missing = [i for i in range(st.meta.pieces) if i not in set(st.list_have())]
                got_any = False

                for piece_idx in missing:
                    if self._stop_download.is_set():
                        return
                    for p in peers:
                        addr = f"{p.ip}:{p.port}"
                        try:
                            with grpc.insecure_channel(addr) as ch:
                                pstub = peer_pb2_grpc.PeerStub(ch)
                                stream = pstub.GetPiece(peer_pb2.GetPieceRequest(
                                    info_hash=st.meta.torrent_id,
                                    piece_index=piece_idx
                                ))
                                buf = bytearray()
                                for part in stream:
                                    buf.extend(part.data)

                            if not buf:
                                continue
                            data = bytes(buf)
                            if not st.validate_piece(piece_idx, data):
                                continue

                            st.save_piece(piece_idx, data)
                            got_any = True
                            pct = st.have_count() / st.meta.pieces * 100.0
                            print(f"⬇️ [{st.meta.torrent_filename}] pieza {piece_idx+1}/{st.meta.pieces} ({pct:.1f}%)")
                            break
                        except grpc.RpcError:
                            continue

                if not got_any:
                    time.sleep(2)
                else:
                    time.sleep(0.2)

        t = threading.Thread(target=loop, daemon=True)
        self._download_threads[ih] = t
        t.start()
        print(f"🧵 Descarga iniciada para {st.meta.torrent_filename} (este peer también sirve piezas).")

    def menu_download_torrent(self):
        torrents = self._list_torrents()
        if not torrents:
            print("\n❌ No hay torrents en tu carpeta torrent/. Primero agrega uno desde el tracker.")
            return

        print("\n=== Torrents locales (torrent/) ===")
        for i, p in enumerate(torrents, start=1):
            print(f"{i}. {os.path.basename(p)}")

        sel = input("Elige número para descargar: ").strip()
        if not sel.isdigit():
            print("Opción inválida.")
            return
        idx = int(sel)
        if idx < 1 or idx > len(torrents):
            print("Fuera de rango.")
            return

        meta = self._load_torrent_file(torrents[idx - 1])
        st = self.get_state_by_hash(meta.torrent_id)
        if not st:
            st = TorrentState(meta, base_dir=self.base_dir, downloads_dir=self.downloads_dir)
            self.set_state(st)
            self.announce(st)
            self._start_torrent_heartbeat(st)

        if st.status() == "seeder":
            print("📤 Ya eres seeder para este torrent (no hace falta descargar).")
            return

        self._start_download(st)

    # ---------- UI ----------
    def menu_debug(self):
        print("\n--- DEBUG ---")
        print(f"peer_id: {self.peer_id}")
        print(f"bind: {self.bind_addr}")
        print(f"tracker: {self.tracker_addr}")
        states = self.list_loaded_states()
        if not states:
            print("torrents cargados: (ninguno)")
            return
        print(f"torrents cargados: {len(states)}")
        for st in states:
            print(f" - {st.meta.torrent_filename} id={st.meta.torrent_id} status={st.status()} have={st.have_count()}/{st.meta.pieces}")

    def menu_progress(self):
        states = self.list_loaded_states()
        if not states:
            print("\nNo hay torrents cargados aún.")
            return

        print("\n=== Torrents cargados ===")
        for i, st in enumerate(states, start=1):
            print(f"{i}. {st.meta.torrent_filename}")

        sel = input("Elige número para ver progreso (tracker): ").strip()
        if not sel.isdigit():
            print("Opción inválida.")
            return
        idx = int(sel)
        if idx < 1 or idx > len(states):
            print("Fuera de rango.")
            return

        st = states[idx - 1]
        prog = self.get_progress(st)
        print("\n--- PROGRESO (según Tracker) ---")
        for p in prog:
            tp = p.total_pieces if p.total_pieces else 0
            pct = (p.pieces_have / tp * 100.0) if tp else 0.0
            online = "ONLINE" if p.online else "OFFLINE"
            print(f"{p.ip}:{p.port} peer={p.peer_id} {p.pieces_have}/{tp} ({pct:.1f}%) {p.status} {online}")

    def run(self):
        while True:
            print("\n========== PEER MENU ==========")
            print("1. Agregar torrent desde Tracker (descargar .torrent.json)")
            print("2. Descargar torrent (de tu carpeta torrent/)")
            print("3. Debuggeo")
            print("4. Ver progreso (tracker)")
            print("5. Salir")
            op = input("Elige opción: ").strip()

            if op == "1":
                try:
                    self.menu_add_torrent_from_tracker()
                except Exception as e:
                    print("❌ Error:", e)
            elif op == "2":
                try:
                    self.menu_download_torrent()
                except Exception as e:
                    print("❌ Error:", e)
            elif op == "3":
                self.menu_debug()
            elif op == "4":
                try:
                    self.menu_progress()
                except Exception as e:
                    print("❌ Error:", e)
            elif op == "5":
                self._stop_download.set()
                self._stop_global_hb.set()
                self._stop_torrent_hb.set()
                print("Saliendo...")
                return
            else:
                print("Opción inválida.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="0.0.0.0:6001", help="ip:puerto para gRPC del peer")
    ap.add_argument("--tracker", default="127.0.0.1:50051", help="ip:puerto del tracker")
    args = ap.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    PeerApp(bind_addr=args.bind, base_dir=base_dir, tracker_addr=args.tracker).run()


if __name__ == "__main__":
    main()
