# torrent_utils.py
import hashlib

def bdecode(data: bytes):
    i = 0
    def parse():
        nonlocal i
        if data[i:i+1] == b"i":
            i += 1
            j = data.index(b"e", i)
            n = int(data[i:j])
            i = j + 1
            return n
        if data[i:i+1] == b"l":
            i += 1
            out = []
            while data[i:i+1] != b"e":
                out.append(parse())
            i += 1
            return out
        if data[i:i+1] == b"d":
            i += 1
            out = {}
            while data[i:i+1] != b"e":
                k = parse()
                v = parse()
                out[k] = v
            i += 1
            return out
        # string: <len>:<bytes>
        if data[i:i+1].isdigit():
            j = data.index(b":", i)
            ln = int(data[i:j])
            i = j + 1
            s = data[i:i+ln]
            i += ln
            return s
        raise ValueError("bdecode inválido")
    val = parse()
    return val

def bencode(x):
    if isinstance(x, int):
        return b"i" + str(x).encode() + b"e"
    if isinstance(x, bytes):
        return str(len(x)).encode() + b":" + x
    if isinstance(x, str):
        b = x.encode("utf-8")
        return str(len(b)).encode() + b":" + b
    if isinstance(x, list):
        return b"l" + b"".join(bencode(i) for i in x) + b"e"
    if isinstance(x, dict):
        # keys must be bytes, sorted
        items = []
        for k in sorted(x.keys()):
            items.append(bencode(k))
            items.append(bencode(x[k]))
        return b"d" + b"".join(items) + b"e"
    raise TypeError("tipo no soportado")

def read_torrent(path):
    raw = path.read_bytes()
    meta = bdecode(raw)
    info = meta[b"info"]
    # infohash = SHA1(bencoded(info))
    infohash = hashlib.sha1(bencode(info)).hexdigest()

    name = info.get(b"name", b"").decode("utf-8", errors="replace")
    length = int(info.get(b"length", 0))
    piece_len = int(info.get(b"piece length", 0))
    pieces_blob = info.get(b"pieces", b"")
    total_pieces = len(pieces_blob) // 20

    return {
        "infohash": infohash,
        "name": name,
        "length": length,
        "piece_length": piece_len,
        "pieces_blob": pieces_blob,
        "total_pieces": total_pieces,
    }

def piece_sha1_expected(pieces_blob: bytes, piece_index: int) -> bytes:
    start = piece_index * 20
    return pieces_blob[start:start+20]
