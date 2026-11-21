"""Every kind of connection a sensor can use, and how each one is read.

Two groups:
  the server reads it   url, modbus, db, influx, suite, calc: read every `Sensor.every` seconds by the ticker;
  it arrives            feed (each minute with the machine's own reading), push (POST /api/sensors), source (an
                        Incoming data source), manual (typed in on the Sensors page).

Every address the server connects to must be on the plant network (plant/netsafety.py), like every other connection.
Protocols the suite doesn't speak itself (MQTT, OPC UA, EtherNet/IP, Siemens S7, BACnet…) come in through a gateway
such as Node-RED as a push or a web address; GATEWAY lists them for the Connection types page."""
import ast
import math
import socket
import struct

from hmi import integrations
from hmi.integrations import ConnError


class ReadError(Exception):
    pass


# key, label, group, what it is for, fields [(key, label, input type, placeholder, help)]
KINDS = [
    ("modbus", "Modbus TCP (PLC or remote I/O)", "read",
     "A register or input on a PLC, a remote I/O block (Moxa ioLogik, Advantech ADAM, Acromag), an IO-Link master or an "
     "analog-to-Ethernet module. Wire the sensor's 0–10 V / 4–20 mA output into an analog input and read its register here.",
     [("host", "Device address", "text", "192.168.100.71", "IP address or name on the plant network"),
      ("port", "Port", "number", "502", "502 unless the device says otherwise"),
      ("unit", "Unit / slave id", "number", "1", "1 for most TCP devices; the slave id behind a gateway"),
      ("table", "Register type", "select:holding=Holding register (4xxxx, function 3)|input=Input register (3xxxx, function 4)|coil=Coil (0xxxx, function 1)|discrete=Discrete input (1xxxx, function 2)", "", ""),
      ("address", "Address", "number", "0", "As the device manual lists it"),
      ("style", "Address style", "select:zero=Starts at 0 (protocol address)|one=Starts at 1 / 40001 style", "", "40001-style addresses are converted"),
      ("format", "Data format", "select:uint16=16-bit unsigned (0–65535)|int16=16-bit signed|uint32=32-bit unsigned (2 registers)|int32=32-bit signed (2 registers)|float32=32-bit float (2 registers)", "", "Coils and discrete inputs are on / off"),
      ("words", "Word order (32-bit)", "select:big=High word first (most devices)|little=Low word first (swapped)", "", ""),
      ("bit", "Bit in the register (optional)", "number", "", "0–15: read one bit of the register as on / off")]),
    ("url", "Web address (JSON)", "read",
     "A device or service that answers a web request with JSON: a smart sensor with a web API, Node-RED, a PLC gateway, "
     "an ESP32 or Raspberry Pi. The server asks it every few seconds.",
     [("url", "Address", "url", "http://192.168.100.60/api/roll", "On the plant network"),
      ("field", "Field", "text", "distance", "Name of the value in the JSON; dots for nested fields (data.ch1.volts)")]),
    ("db", "Database table", "read",
     "The newest row of a table in the machine data database or the asset database, e.g. a data logger or SCADA "
     "historian table. Read-only, like every outside database.",
     [("db", "Database", "select:machines=Machine data database|assets=Asset database", "", ""),
      ("table", "Table", "text", "sensor_log", ""),
      ("column", "Value column", "text", "value", ""),
      ("orderCol", "Newest row by", "text", "logged_at", "A time stamp or ID column"),
      ("fcol", "Only rows where (column, optional)", "text", "tag", "e.g. a tag or channel column"),
      ("fval", "…equals", "text", "BARB_ROLL", ""),
      ("maxAge", "No new row for this many minutes = no data", "number", "3", "0 = never")]),
    ("influx", "InfluxDB query", "read",
     "The newest value from InfluxDB (Admin › Connections › InfluxDB), e.g. sensors logged by Telegraf or Node-RED.",
     [("query", "Query", "textarea", 'from(bucket: "sensors") |> range(start: -5m) |> filter(fn: (r) => r._measurement == "roll" and r.line == "Barbara") |> last()', "Flux (2.x) or InfluxQL (1.x / 3.x); read only"),
      ("field", "Field / column", "text", "_value", "Blank = the first number")]),
    ("feed", "Extra field of the machine's own reading", "arrives",
     "Another field in the reading the machine already sends for its speed (web address, pushed or InfluxDB feed). "
     "Read once a minute with the speed. Pleaters only; for a database-table feed use Database table instead.",
     [("field", "Field", "text", "roll_mm", "Dots for nested fields")]),
    ("push", "Sent to the server", "arrives",
     "Another system posts the value whenever it changes or on a timer: Node-RED, a PLC with an HTTP block, an ESP32, a "
     "script. POST JSON to /api/sensors with the header X-Feed-Key (the machine feed key in Admin › Connections).",
     []),
    ("source", "Incoming data source", "arrives",
     "A number field from an Admin › Connections › Incoming data source (Other readings), e.g. a vision system that also "
     "reports a temperature.",
     [("source", "Data source", "text", "vision-barbara", "Its name in Incoming data"),
      ("field", "Field", "text", "temp_c", "Dots for nested fields"),
      ("line", "Only records for line (optional)", "text", "", "When the source sends several lines")]),
    ("suite", "A value the suite already has", "read",
     "Something the suite works out for the machine, so you can alarm on it: speed, filters made, uptime, how long it has "
     "been stopped, time since the last QC check…",
     [("what", "Value", "select:ppm=Speed (machine's speed unit)|running=Running (on / off)|count=Filters made this shift|mo_count=Filters made on this MO|mo_left=Filters left on the MO|uptime=Uptime this shift (%)|down=Downtime this shift (min)|stop=Minutes stopped right now|qc=Minutes since the last QC check|co=In a changeover (on / off)", "", "")]),
    ("calc", "Calculated from other sensors", "read",
     "A formula over other sensors' values by their short id: differences, averages, ratios. "
     "Allowed: + − × ÷ ** ( ), min, max, abs, round, avg, sqrt.",
     [("formula", "Formula", "text", "(barb_roll_l + barb_roll_r) / 2", "Other sensors' short ids")]),
    ("manual", "Typed in", "arrives",
     "Someone types the reading on the Sensors page: a gauge read by hand, a lab result, a count.",
     []),
]
KIND = {k[0]: k for k in KINDS}
SERVER_READS = {k for k, _, g, _, _ in KINDS if g == "read"}
GATEWAY = [("MQTT", "Subscribe in Node-RED (mqtt in node), then push to /api/sensors or serve it on a web address."),
           ("OPC UA", "Node-RED (node-red-contrib-opcua) or Kepware's REST server → push or web address."),
           ("EtherNet/IP (Allen-Bradley)", "Node-RED (node-red-contrib-cip-ethernet-ip), or map the tag to a Modbus register in the PLC."),
           ("Siemens S7", "Node-RED (node-red-contrib-s7), or Modbus TCP from the PLC (MB_SERVER)."),
           ("IO-Link", "Most IO-Link masters also speak Modbus TCP or have a JSON web API: use those directly."),
           ("4–20 mA / 0–10 V wired straight to a sensor", "Into a remote I/O block (Modbus TCP) or a PLC analog card, then Modbus TCP."),
           ("Serial / RS-485 Modbus RTU", "A Modbus RTU-to-TCP gateway (Moxa MGate, USR-TCP232), then Modbus TCP here.")]
PRESETS = [("0-10V", "0–10 V", 0, 10, "V"), ("0-5V", "0–5 V", 0, 5, "V"), ("1-5V", "1–5 V", 1, 5, "V"), ("4-20mA", "4–20 mA", 4, 20, "mA"),
           ("0-20mA", "0–20 mA", 0, 20, "mA"), ("0-27648", "Siemens analog 0–27648", 0, 27648, "counts"),
           ("0-32767", "16-bit 0–32767", 0, 32767, "counts"), ("0-65535", "16-bit 0–65535", 0, 65535, "counts"),
           ("0-4095", "12-bit 0–4095", 0, 4095, "counts"), ("0-1023", "10-bit 0–1023", 0, 1023, "counts")]


def fields_for(kind):
    out = []
    for key, label, typ, ph, help_ in (KIND.get(kind) or KINDS[0])[4]:
        opts = []
        if typ.startswith("select:"):
            opts = [tuple(x.split("=", 1)) for x in typ[7:].split("|")]
            typ = "select"
        out.append({"key": key, "label": label, "type": typ, "ph": ph, "help": help_, "opts": opts})
    return out


def all_kinds():
    return [{"key": k, "label": l, "group": g, "about": a, "fields": fields_for(k)} for k, l, g, a, _ in KINDS]


def clean_config(kind, p):
    """The kind's own settings from a form (only its own fields)."""
    out = {}
    for f in fields_for(kind):
        v = str(p.get(f"c_{kind}_{f['key']}", "") or "").strip()
        if f["type"] == "select" and v not in {o[0] for o in f["opts"]}:
            v = f["opts"][0][0]
        out[f["key"]] = v[:2000]
    return out


def check_config(kind, c, sensor_name=""):
    """Plain-language problem with the settings, or ''."""
    from plant import netsafety
    if kind not in KIND:
        return "Pick how the sensor is connected."
    try:
        if kind == "modbus":
            netsafety.check_host(c.get("host"), "The device address")
            for k, lo, hi in (("port", 1, 65535), ("unit", 0, 255), ("address", 0, 465535)):
                n = _int(c.get(k, ""))
                if n is None or not lo <= n <= hi:
                    return f"{dict((x[0], x[1]) for x in KIND['modbus'][4])[k]}: enter a number from {lo} to {hi}."
            _addr(c)
            if c.get("bit") not in ("", None) and not 0 <= (_int(c["bit"]) if _int(c["bit"]) is not None else -1) <= 15:
                return "Bit: 0 to 15, or leave it blank."
        elif kind == "url":
            netsafety.check_url(c.get("url"), "The address")
            if not c.get("field"):
                return "Enter the field that holds the value."
        elif kind == "db":
            for k in ("table", "column", "orderCol"):
                if not c.get(k):
                    return "Enter the table, the value column and the column that tells which row is newest."
        elif kind == "influx" and not c.get("query"):
            return "Enter the query."
        elif kind in ("feed", "source") and not c.get("field"):
            return "Enter the field that holds the value."
        elif kind == "source" and not c.get("source"):
            return "Enter the data source's name."
        elif kind == "calc":
            names = formula_names(c.get("formula", ""))
            if sensor_name and sensor_name in names:
                return "A formula can't use the sensor's own value."
    except netsafety.NotPrivate as e:
        return str(e)
    except ReadError as e:
        return str(e)
    return ""


def _int(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ Modbus TCP
FN = {"coil": 1, "discrete": 2, "holding": 3, "input": 4}
PREFIX = {"coil": 0, "discrete": 1, "input": 3, "holding": 4}


def _addr(c):
    a = _int(c.get("address"))
    if a is None or a < 0:
        raise ReadError("Enter the register address.")
    if c.get("style") == "one":
        p = PREFIX[c.get("table") or "holding"]
        if a >= 10000 and str(a)[0] == str(p) and len(str(a)) in (5, 6):   # 40001 / 400001 style
            a = int(str(a)[1:])
        a -= 1
    if not 0 <= a <= 65535:
        raise ReadError("That address is outside 0–65535 once converted.")
    return a


def _recv(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ReadError("The device closed the connection.")
        buf += chunk
    return buf


EXC = {1: "illegal function", 2: "illegal data address (no such register)", 3: "illegal data value", 4: "device failure",
       6: "device busy", 10: "gateway path unavailable", 11: "gateway target didn't respond"}


def modbus_request(host, port, unit, fn, addr, count, timeout=2.0, tid=1):
    """One Modbus TCP read. Returns the registers (fn 3/4) or bits (fn 1/2) as a list of ints."""
    from plant import netsafety
    try:
        netsafety.check_host(host, "The device address")
    except netsafety.NotPrivate as e:
        raise ReadError(str(e))
    pdu = struct.pack(">BHH", fn, addr, count)
    adu = struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(adu)
            head = _recv(s, 7)
            _tid, _pid, length, _unit = struct.unpack(">HHHB", head)
            body = _recv(s, length - 1)
    except (socket.timeout, TimeoutError):
        raise ReadError(f"{host}:{port} didn't answer in {timeout:g} s.")
    except OSError as e:
        raise ReadError(f"Couldn't reach {host}:{port} ({e.strerror or e}).")
    if body[0] & 0x80:
        code = body[1] if len(body) > 1 else 0
        raise ReadError(f"The device refused the read: {EXC.get(code, f'error {code}')}.")
    data = body[2:2 + body[1]]
    if fn in (3, 4):
        return list(struct.unpack(f">{len(data) // 2}H", data))[:count]
    bits = []
    for byte in data:
        bits += [(byte >> i) & 1 for i in range(8)]
    return bits[:count]


def decode(regs, fmt, words="big"):
    if fmt in ("uint16", "int16"):
        v = regs[0]
        return v - 65536 if fmt == "int16" and v >= 32768 else v
    hi, lo = (regs[0], regs[1]) if words != "little" else (regs[1], regs[0])
    raw = struct.pack(">HH", hi, lo)
    return struct.unpack({"uint32": ">I", "int32": ">i", "float32": ">f"}[fmt], raw)[0]


def read_modbus(c):
    table = c.get("table") or "holding"
    fn = FN[table]
    fmt = c.get("format") or "uint16"
    bit = _int(c.get("bit")) if str(c.get("bit") or "").strip() else None
    count = 1 if fn in (1, 2) or fmt in ("uint16", "int16") or bit is not None else 2
    vals = modbus_request(c.get("host", "").strip(), _int(c.get("port")) or 502, _int(c.get("unit")) or 0, fn, _addr(c), count)
    if fn in (1, 2):
        return float(vals[0])
    if bit is not None:
        return float((vals[0] >> bit) & 1)
    return float(decode(vals, fmt, c.get("words")))


# ------------------------------------------------------------------ formulas
FUNCS = {"min": min, "max": max, "abs": abs, "round": round, "sqrt": math.sqrt, "avg": lambda *a: sum(a) / len(a)}
_BIN = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
        ast.Pow: lambda a, b: a ** b, ast.Mod: lambda a, b: a % b}


def formula_names(expr):
    try:
        tree = ast.parse(str(expr or ""), mode="eval")
    except SyntaxError:
        raise ReadError("The formula isn't written right (check the brackets and operators).")
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in FUNCS}


def evaluate(expr, values):
    """A formula over sensor values. Only numbers, the sensors' short ids, + - * / ** % and FUNCS."""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.Name):
            if n.id not in values:
                raise ReadError(f'There is no sensor "{n.id}".')
            if values[n.id] is None:
                raise ReadError(f'"{n.id}" has no reading yet.')
            return float(values[n.id])
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left), ev(n.right)
            if type(n.op) is ast.Pow and abs(b) > 10:
                raise ReadError("Powers above 10 aren't allowed.")
            return _BIN[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in FUNCS and not n.keywords and 0 < len(n.args) <= 20:
            return float(FUNCS[n.func.id](*[ev(a) for a in n.args]))
        raise ReadError("Formulas can use numbers, sensor ids, + − * / ** %, brackets and min, max, abs, round, avg, sqrt.")
    try:
        tree = ast.parse(str(expr or ""), mode="eval")
    except SyntaxError:
        raise ReadError("The formula isn't written right (check the brackets and operators).")
    try:
        return ev(tree)
    except ZeroDivisionError:
        raise ReadError("The formula divides by zero right now.")
    except (OverflowError, ValueError) as e:
        raise ReadError(f"The formula can't be worked out: {e}.")


# ------------------------------------------------------------------ reading
def number(v):
    """A reading as a number: numbers, numeric text, and High/Low words (1 / 0)."""
    from hmi import machinedata
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v) if v == v and abs(v) != float("inf") else None
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8", "replace")
    try:
        return float(str(v).strip().replace(",", ""))
    except ValueError:
        s = machinedata.signal(v)
        return None if s is None else (1.0 if s else 0.0)


def suite_value(machine, what, t):
    from hmi.models import LineState, ShiftRun
    sh = ShiftRun.objects.filter(active=True, line=machine).select_related("mo").first() if machine else None
    if what == "ppm":
        ls = LineState.objects.filter(machine__name=machine).first()
        return (ls.feed or {}).get("ppm") if ls else None
    if what == "running":
        return 1.0 if sh and sh.state == "running" else 0.0
    if what == "co":
        return 1.0 if sh and sh.state == "changeover" else 0.0
    if not sh:
        raise ReadError(f"No shift is running on {machine or 'that machine'}.")
    if what == "count":
        return sh.count
    if what == "mo_count":
        return sh.mo_count
    if what == "mo_left":
        if not sh.mo:
            raise ReadError(f"{machine} has no MO running.")
        return max(0, (sh.mo.qty or 0) - (sh.mo.produced or 0))
    if what == "uptime":
        return sh.uptime
    if what == "down":
        return sh.down_mins or 0
    if what == "stop":
        return (t - sh.stop_start).total_seconds() / 60 if sh.state == "stopped" and sh.stop_start else 0.0
    if what == "qc":
        last = sh.qc_last or sh.start
        return (t - last).total_seconds() / 60
    raise ReadError("Pick the value.")


def read(sensor, t, values=None):
    """One raw reading for the kinds the server reads. Raises ReadError (or ConnError) with what went wrong."""
    c = sensor.config or {}
    k = sensor.kind
    try:
        if k == "modbus":
            return read_modbus(c)
        if k == "url":
            data = integrations.get_json(c.get("url", ""))
            v = integrations.pick(data, c.get("field"))
            if v is None:
                raise ReadError(f'No "{c.get("field")}" field in the answer.')
            return v
        if k == "db":
            from hmi import machinedata
            where = [{"col": c["fcol"], "val": c.get("fval", "")}] if c.get("fcol") else []
            row, newest = machinedata.latest_row(c.get("db") or "machines", c.get("table", ""), [c.get("column", "")], c.get("orderCol", ""), where=where)
            st = sensor.state or {}
            mark = machinedata.text(newest, 60)
            since = st.get("since") if st.get("row") == mark and st.get("since") else t.timestamp()
            sensor.state = {**st, "row": mark, "since": since}
            age = _int(c.get("maxAge") or 0) or 0
            if age and t.timestamp() - since > age * 60:
                raise ReadError(f"No new row for {round((t.timestamp() - since) / 60)} min (newest: {mark}).")
            return row[c["column"]]
        if k == "influx":
            from hmi import influx
            rows = influx.query(c.get("query", ""))
            sample, _when = influx.latest_sample(rows)
            if not sample:
                raise ReadError("The query returned no rows.")
            f = c.get("field")
            if f:
                if f not in sample:
                    raise ReadError(f'No "{f}" in the result (it has {", ".join(list(sample)[:8])}).')
                return sample[f]
            for key, v in sample.items():
                if key not in ("_time", "time") and number(v) is not None:
                    return v
            raise ReadError("No number in the result.")
        if k == "suite":
            return suite_value(sensor.machine, c.get("what"), t)
        if k == "calc":
            return evaluate(c.get("formula", ""), values or {})
    except ConnError as e:
        raise ReadError(str(e))
    except Exception as e:  # noqa: BLE001 - an outside system's own error (InfluxDB, a database)
        if isinstance(e, ReadError):
            raise
        raise ReadError(str(e)[:250] or type(e).__name__)
    raise ReadError("This sensor's value arrives on its own; the server doesn't read it.")
