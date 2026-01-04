import os
import json
import math
import hashlib
import argparse

PEDAZO_TAM = 102400  # bytes, igual que tu profe
HASH_ALGORITHM = "md5"


def md5_hex(data: bytes) -> str:
    h = hashlib.new(HASH_ALGORITHM)
    h.update(data)
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("tracker_ip", help="IP del tracker (ej 127.0.0.1)")
    p.add_argument("puerto_tracker", type=int, help="Puerto del tracker (ej 50051)")
    p.add_argument("file_name", help="Nombre del archivo dentro de archivos/ (ej video.mp4)")
    args = p.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    archivos_dir = os.path.join(base_dir, "archivos")
    torrents_dir = os.path.join(base_dir, "torrent")
    os.makedirs(torrents_dir, exist_ok=True)

    file_path = os.path.join(archivos_dir, args.file_name)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"No existe: {file_path}")

    file_obj_name = os.path.basename(file_path)

    # fileName sin extensión (para nombrar el torrent)
    name_no_ext, _ = os.path.splitext(file_obj_name)

    file_size = os.path.getsize(file_path)
    pieces_qty = int(math.ceil(file_size / PEDAZO_TAM))
    last_piece = 0
    if pieces_qty * PEDAZO_TAM > file_size:
        last_piece = int(file_size - (pieces_qty - 1) * PEDAZO_TAM)
    else:
        last_piece = PEDAZO_TAM

    # checksum por pieza (MD5)
    checksum = []
    with open(file_path, "rb") as f:
        for i in range(pieces_qty):
            if i < pieces_qty - 1:
                data = f.read(PEDAZO_TAM)
            else:
                data = f.read(last_piece)
            checksum.append(md5_hex(data))

    torrent_obj = {
        "tracker": args.tracker_ip,
        "puertoTracker": args.puerto_tracker,
        "pieces": pieces_qty,
        "lastPiece": last_piece,
        "filepath": f"archivos/{args.file_name}",  # igual que tu profe (ruta relativa)
        "name": file_obj_name,
        "checksum": checksum,
        "id": md5_hex(name_no_ext.encode("utf-8")),  # igual idea: ID único del nombre
    }

    out_path = os.path.join(torrents_dir, f"{name_no_ext}.torrent.json")
    with open(out_path, "w", encoding="utf-8") as f:
        # tu profe escribe todo en 1 línea; aquí lo dejamos 1 línea para igualarlo
        f.write(json.dumps(torrent_obj, separators=(",", ":")))

    print("Torrent creado!")
    print("Archivo:", out_path)
    print("ID:", torrent_obj["id"])
    print("Pieces:", pieces_qty, "LastPiece:", last_piece)


if __name__ == "__main__":
    main()
