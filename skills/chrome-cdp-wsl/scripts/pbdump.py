import sys, struct

def read_varint(b, i):
    result = 0
    shift = 0
    while True:
        byte = b[i]
        i += 1
        result |= (byte & 0x7f) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return result, i

def dump(b, indent=0, depth=0):
    i = 0
    out = []
    while i < len(b):
        try:
            tag, i = read_varint(b, i)
        except IndexError:
            out.append(' '*indent + f'<trailing {len(b)-i} bytes: {b[i:].hex()}>')
            break
        field_num = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 0:
            val, i = read_varint(b, i)
            out.append(' '*indent + f'field {field_num} (varint) = {val}')
        elif wire_type == 1:
            val = struct.unpack('<d', b[i:i+8])[0]
            out.append(' '*indent + f'field {field_num} (64bit) = {val} raw={b[i:i+8].hex()}')
            i += 8
        elif wire_type == 2:
            ln, i = read_varint(b, i)
            chunk = b[i:i+ln]
            i += ln
            out.append(' '*indent + f'field {field_num} (bytes len={ln}) = {chunk.hex()}')
            # try to recursively parse
            try:
                sub = dump(chunk, indent+4, depth+1)
                if sub:
                    out.append(' '*(indent+2) + '-> as submessage:')
                    out.extend(sub)
            except Exception as e:
                pass
            # try as utf8
            try:
                s = chunk.decode('utf-8')
                if s.isprintable():
                    out.append(' '*(indent+2) + f'-> as utf8: {s!r}')
            except Exception:
                pass
        elif wire_type == 5:
            val = struct.unpack('<f', b[i:i+4])[0]
            out.append(' '*indent + f'field {field_num} (32bit) = {val} raw={b[i:i+4].hex()}')
            i += 4
        else:
            out.append(' '*indent + f'field {field_num} unknown wiretype {wire_type}')
            break
    return out

if __name__ == '__main__':
    hexstr = sys.argv[1]
    b = bytes.fromhex(hexstr)
    for line in dump(b):
        print(line)
