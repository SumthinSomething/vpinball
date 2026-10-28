#!/usr/bin/env python3
"""Smoke test for the embedded MCP server.

Launches VPinballX in live edit mode on a copy of the blank table and runs the steps listed in STEPS in order:
protocol edge cases, part / asset / script / table edits with undo and redo, clones from the example table,
a playtest with shots, plunges, sweeps, jobs and scenarios, the editor view actions, the package export and
the geometry and ramp tools. Exits non zero on the first failed check.

Usage: tools/mcp_smoke.py <VPinballX executable or .app> [--port 2114] [--token xxx] [--keep] [--list] [--until step]
The MCP server must be enabled in VPinballX.ini ([MCP] Enable = 1).
"""

import argparse
import json
import zipfile
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RAMP_PLAN_POINTS = [[480, 1400, 0], [540, 950, 110], [700, 500, 125], [450, 380, 125], [250, 600, 105], [230, 1100, 60]]


class Client:
    def __init__(self, port, token):
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.health = f"http://127.0.0.1:{port}/health"
        self.token = token
        self.next_id = 0
        self.session = None
        self.last_body = {}

    def rpc(self, method, params=None, timeout=120, protocol=None):
        self.next_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if protocol:
            headers["MCP-Protocol-Version"] = protocol
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session = sid
            return json.loads(resp.read().decode())

    def call(self, name, args=None, timeout=120):
        r = self.rpc("tools/call", {"name": name, "arguments": args or {}}, timeout)
        if "error" in r:
            raise RuntimeError(f"{name}: {r['error']['message']}")
        res = r["result"]
        text = next((c["text"] for c in res["content"] if c["type"] == "text"), "")
        if res.get("isError"):
            raise RuntimeError(f"{name}: {text}")
        try:
            return json.loads(text)
        except ValueError:
            return text

    def raw(self, body, method, extra_headers=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(extra_headers or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url, data=body if method == "POST" else None, headers=headers, method=method)
        self.last_body = {}
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read().decode()
                try:
                    self.last_body = json.loads(data) if data else {}
                except ValueError:
                    pass
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    def wait_ready(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.health, timeout=1) as resp:
                    if json.loads(resp.read().decode()).get("sessionActive"):
                        return True
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.5)
        return False


def wait_listening_port(log_path, default, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with open(log_path, errors="replace") as f:
                m = re.search(r"MCP server listening on http://127\.0\.0\.1:(\d+)/mcp", f.read())
                if m:
                    return int(m.group(1))
        except OSError:
            pass
        time.sleep(0.2)
    return default


def resolve_executable(path):
    if path.endswith(".app") and os.path.isdir(path):
        name = os.path.splitext(os.path.basename(path))[0]
        return os.path.join(path, "Contents", "MacOS", name)
    return path


def find_asset(executable, name, extra=()):
    candidates = [
        os.path.join(ROOT, "src", "assets", name),
        os.path.join(os.path.dirname(executable), "..", "Resources", "assets", name),
        os.path.join(os.path.dirname(executable), "assets", name),
        *extra,
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FileNotFoundError(f"{name} not found")


CHECKS = 0


def check(cond, message):
    global CHECKS
    CHECKS += 1
    print(f"[{'ok  ' if cond else 'FAIL'}] {message}")
    if not cond:
        raise AssertionError(message)


def expect_error(fn, message, *needles):
    """The call must fail; every needle must appear in the error text. Returns the error text."""
    try:
        fn()
    except RuntimeError as e:
        text = str(e)
        check(all(n in text for n in needles), message if not needles else f"{message} ({'; '.join(needles)})")
        return text
    check(False, message)


class Smoke:
    """The running VPX instance, its MCP client and the state the steps hand to each other."""

    def __init__(self, client, port, executable, workdir, table):
        self.client = client
        self.port = port
        self.executable = executable
        self.workdir = workdir
        self.table = table
        self.export_path = os.path.join(workdir, "export.png")
        self.bumpers_at_start = 0
        self.first_image = None
        self.donor = None
        self.donor_names = []
        self.cloned = []
        self.target = (0, 0)

    def call(self, name, args=None, timeout=120):
        return self.client.call(name, args, timeout)

    def status(self):
        return self.call("vpx_status")

    def wait_status(self, predicate, seconds, what):
        deadline = time.time() + seconds
        last = None
        while time.time() < deadline:
            try:
                last = self.status()
                if predicate(last):
                    return last
            except (RuntimeError, urllib.error.URLError, OSError):
                pass
            time.sleep(0.5)
        raise AssertionError(f"timed out waiting for {what}: {last}")

    def undo(self, steps=1):
        result = None
        for _ in range(steps):
            result = self.call("vpx_editor", {"action": "undo"})
        return result

    def redo(self):
        return self.call("vpx_editor", {"action": "redo"})

    def count(self, part_type=None):
        return self.call("vpx_part", {"action": "list", "type": part_type} if part_type else {"action": "list"})["count"]

    def part_names(self, part_type=None):
        return [p["name"] for p in self.call("vpx_part", {"action": "list", "type": part_type} if part_type else {"action": "list"})["parts"]]

    def fields(self, name, *fields):
        return self.call("vpx_part", {"action": "get", "name": name, "fields": list(fields)})["data"]

    def field(self, name, field):
        return self.fields(name, field)[field]

    def render(self):
        self.call("vpx_screenshot", {"maxSize": 64})

    def shot_at_target(self, **extra):
        args = {"action": "shot", "x": self.target[0], "y": 1500, "targetX": self.target[0], "targetY": self.target[1], "ms": 1500}
        args.update(extra)
        return args


def step_protocol(t):
    c = t.client
    check(c.wait_ready(60), "VPX started and the MCP server answers")
    init = c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "mcp_smoke", "version": "1"}})
    check(init["result"]["serverInfo"]["name"] == "vpinball", "initialize")
    listed = {tool["name"]: tool for tool in c.rpc("tools/list")["result"]["tools"]}
    check("vpx_sim" in listed and "vpx_part" in listed, f"{len(listed)} tools listed")
    fingerprint = t.status()["server"]["toolsFingerprint"]
    check(init["result"]["serverInfo"]["version"].endswith("+" + fingerprint), "status reports the tools fingerprint of serverInfo")
    topic = t.call("vpx_status", {"guide": "sim"})
    check("flipWhenAlong" in topic["guide"] and "parts" in topic["topics"], f"guide topics served: {topic['topics']}")

    r = c.rpc("tools/call", {"name": "vpx_nope", "arguments": {}})
    check("error" in r and "Unknown tool" in r["error"]["message"], "unknown tool is a JSON-RPC error")
    r = c.rpc("no/such/method")
    check("error" in r and r["error"]["code"] == -32601, "unknown method is a JSON-RPC error")
    check(c.raw(b"{not json", "POST") == 200 and c.last_body.get("error", {}).get("code") == -32700, "invalid JSON answers a parse error")
    check(c.raw(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(), "POST") == 202, "notifications are accepted with 202")
    check(c.raw(b"", "GET") == 405, "GET on the endpoint is 405 (no SSE stream)")
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    port = t.port
    check(c.raw(ping, "POST", {"Origin": "null"}) == 403, "Origin null (sandboxed iframe, file:// page) is refused")
    check(c.raw(ping, "POST", {"Origin": "http://localhost.example.com"}) == 403, "a non loopback origin is refused")
    check(c.raw(ping, "POST", {"Host": f"rebind.example.com:{port}"}) == 403, "a non loopback Host (DNS rebinding) is refused")
    check(c.raw(ping, "POST", {"Content-Type": "text/plain"}) == 415, "a text/plain POST (no CORS preflight) is refused")
    check(c.raw(ping, "POST", {"Origin": f"http://127.0.0.1:{port}"}) == 200, "a loopback origin is accepted")
    check(listed["vpx_status"]["annotations"]["readOnlyHint"] and listed["vpx_part"]["annotations"]["destructiveHint"] and listed["vpx_sim"]["title"], "tools carry annotations and titles")
    new_proto = c.rpc("tools/call", {"name": "vpx_status", "arguments": {}}, protocol="2025-06-18")["result"]
    old_proto = c.rpc("tools/call", {"name": "vpx_status", "arguments": {}}, protocol="2025-03-26")["result"]
    check(isinstance(new_proto.get("structuredContent"), dict) and "server" in new_proto["structuredContent"] and "structuredContent" not in old_proto, "structuredContent sent to 2025-06-18 clients only")

    t.wait_status(lambda s: s["editorOpened"] and s["playMode"] == "FullEdit", 30, "editor session")
    t.bumpers_at_start = t.count("Bumper")
    t.first_image = t.call("vpx_image", {"action": "list"})["images"][0]["name"]


def step_bare_parts(t):
    before = t.bumpers_at_start
    r = t.call("vpx_part", {"action": "add", "type": "Bumper", "x": 475, "y": 900, "name": "SmokeBare", "data": {"force": 20, "radius": 45}})
    check(r["name"] == "SmokeBare" and "force" in r["applied"], "bare bumper added")
    got = t.call("vpx_part", {"action": "get", "name": "SmokeBare", "fields": ["force", "radius", "center", "nope"]})
    check(got["data"]["force"] == 20 and got["data"]["radius"] == 45 and got["data"]["center"]["x"] == 475 and got["unknown"] == ["nope"], "bare bumper fields read back, unknown field reported")
    expect_error(lambda: t.call("vpx_part", {"action": "set", "name": "SmokeBare", "data": {"Radiuss": 50}}), "unknown field name is rejected with a suggestion", "Unknown field 'Radiuss'", "'radius'")
    r = t.call("vpx_part", {"action": "set", "name": "SmokeBare", "data": {"Radius": 50}})
    check(r["applied"] == ["radius"] and r["mapped"]["Radius"] == "radius", "script-style field name mapped onto the stored one")
    t.undo()
    r = t.call("vpx_part", {"action": "set", "name": "SmokeBare", "data": {"threshold": -1, "timer_interval": -5}})
    got = t.fields("SmokeBare", "threshold", "timer_interval")
    check("usual range" in r["valueNotes"]["threshold"] and "clamped" in r["valueNotes"]["timer_interval"] and got == {"threshold": -1, "timer_interval": -2}, f"out of range values: usual range noted, enforced range clamped ({r['valueNotes']})")
    t.undo()
    gates_before = t.count("Gate")
    expect_error(lambda: t.call("vpx_part", {"action": "add", "type": "Gate", "x": 475, "y": 900, "name": "SmokeBadGate", "data": {"type": 9}}), "an unknown enum value is refused", "takes 1 wire W")
    check(t.count("Gate") == gates_before, "nothing was created for the refused enum value")
    expect_error(lambda: t.call("vpx_part", {"action": "add", "type": "Bumper", "x": 475, "y": 900, "name": "SmokeBad", "data": {"Forcee": 20}}), "add with an unknown field is refused")
    check(t.count("Bumper") == before + 1, "add with an unknown field creates nothing")
    t.undo()
    check(t.count("Bumper") == before, "undo removed the bare bumper")


def step_geometry_rebuilds(t):
    """Property changes that regenerate meshes, physics or render buffers (each used to crash)."""
    regen = [
        ("Primitive", {"size": {"x": 100, "y": 100, "z": 60}}, [("sides", 3), ("sides", 12), ("sides", 100), ("sides", 2)], ("sides", 3)),
        ("Trigger", {}, [("shape", v) for v in (1, 2, 3, 4, 5, 0, 2)], ("shape", 2)),
        ("Wall", {}, [("height_top", 120), ("height_bottom", 20), ("height_top", 50)], ("height_top", 50)),
        ("Ramp", {}, [("height_top", 200), ("width_top", 150), ("type", 1), ("type", 0)], ("height_top", 200)),
        ("Bumper", {}, [("radius", 20), ("radius", 60), ("height_scale", 120)], ("radius", 60)),
        ("Flipper", {}, [("flipper_radius_max", 80), ("flipper_radius_max", 150), ("base_radius", 30)], ("flipper_radius_max", 150)),
        ("Kicker", {}, [("radius", 40), ("type", 1), ("type", 2)], ("radius", 40)),
        ("Rubber", {}, [("thickness", 4), ("thickness", 20), ("height_top", 40)], ("thickness", 20)),
        ("Gate", {}, [("length", 60), ("type", 1), ("type", 3), ("show_bracket", False)], ("length", 60)),
        ("Spinner", {}, [("length", 60), ("height", 80)], ("length", 60)),
        ("HitTarget", {}, [("target_type", 2), ("target_type", 6), ("target_type", 1), ("size", {"x": 60})], ("target_type", 1)),
        ("Light", {}, [("radius", 80), ("radius", 200)], ("radius", 200)),
    ]
    names = []
    for i, (ptype, init, changes, (verify_prop, verify_value)) in enumerate(regen):
        name = f"Regen{ptype}"
        r = t.call("vpx_part", {"action": "add", "type": ptype, "x": 200 + (i % 4) * 150, "y": 500 + (i // 4) * 200, "name": name, "data": init})
        check(r["name"] == name, f"added {name}")
        names.append(name)
        for prop, value in changes:
            r = t.call("vpx_part", {"action": "set", "name": name, "data": {prop: value}})
            if prop not in r["applied"] or "warning" in r:
                raise AssertionError(f"{name}.{prop} = {value} not applied: {r}")
            t.render()
        got = t.field(name, verify_prop)
        check(got == verify_value, f"{name}: {len(changes)} field changes rendered, {verify_prop} reads back {got}")
    got = t.field("RegenHitTarget", "size")
    check(got["x"] == 60 and got["z"] != 0, f"partial object merge kept the other components (size {got})")
    r = t.call("vpx_part", {"action": "delete", "names": names})
    check(r["deleted"] == len(names), f"deleted the {len(names)} regenerated parts")
    t.undo()
    got = t.field("RegenPrimitive", "sides")
    check(got == 3, f"undo restored the deleted parts with their edited values (sides {got})")
    t.render()
    t.call("vpx_part", {"action": "delete", "names": names})
    check(t.count("Bumper") == t.bumpers_at_start, "regenerated parts removed again")


def step_stored_definitions(t):
    raw = t.call("vpx_part", {"action": "get", "name": "Peg1"})["data"]
    check(raw.get("use_mesh") is True and len(raw) > 30, f"definition of a mesh primitive has {len(raw)} fields")
    t.call("vpx_part", {"action": "set", "name": "Peg1", "data": {"depth_bias": 5, "ui_edge_factor": 0.5}})
    raw = t.call("vpx_part", {"action": "get", "name": "Peg1"})["data"]
    check(raw["depth_bias"] == 5 and raw["ui_edge_factor"] == 0.5 and raw["use_mesh"] is True, "stored fields set and reloaded in place, mesh kept")
    t.render()
    wall_top = t.field("Wall25", "height_top")
    t.call("vpx_part", {"action": "set", "name": "Wall25", "data": {"height_top": wall_top + 30, "name": "Wall25Renamed"}})
    check(t.field("Wall25Renamed", "height_top") == wall_top + 30, "set changed a wall height and renamed it through the table")
    t.undo()
    check(t.field("Wall25", "height_top") == wall_top, "undo restored the wall's height and name from the snapshot")
    t.call("vpx_part", {"action": "set", "name": "Wall25", "data": {"height_bottom": 10}})
    t.undo()
    check(t.field("Wall25", "height_bottom") == 0, "undo restored a field edit")
    r = t.call("vpx_part", {"action": "set", "name": "Wall25", "data": {"HeightBottom": 10}})
    check(r["applied"] == ["height_bottom"] and r["mapped"] == {"HeightBottom": "height_bottom"}, "script-style names map onto the stored fields")
    check(t.fields("Wall25", "HeightBottom")["height_bottom"] == 10, "get maps script-style field names too")
    t.undo()
    expect_error(lambda: t.call("vpx_part", {"action": "set", "name": "Wall25", "data": {"HeightTopp": 10}}), "unknown field error suggests fields and an example", "'HeightTopp'", "height_top", "data: {")


def step_schemas(t):
    schema = t.call("vpx_part", {"action": "schema", "type": "Kicker"})
    check(schema["fields"]["type"]["values"]["1"] == "hole" and "clockwise" in schema["fields"]["orientation"].get("meaning", ""), "schema explains enum values and directions")
    gate = t.call("vpx_part", {"action": "schema", "type": "Gate"})["fields"]
    check(gate["damping"]["range"] == {"min": 0, "max": 1, "enforced": True, "note": gate["damping"]["range"]["note"]} and gate["elasticity"]["range"]["enforced"] is False and "RADIANS" in gate["angle_max"]["meaning"], "schema carries field ranges and units")
    schema = t.call("vpx_part", {"action": "schema", "type": "Spinner"})
    check("angle_max" in schema["fields"] and schema["fields"]["angle_max"]["type"] == "number", f"schema of a type with no instance lists {len(schema['fields'])} fields with defaults")
    schema = t.call("vpx_part", {"action": "schema", "type": "Flipper"})
    check("example" in schema["fields"]["start_angle"], "schema of an instantiated type carries example values")


def step_script_and_audit(t):
    audit = t.call("vpx_table", {"action": "audit"})
    check(isinstance(audit["errors"], list) and isinstance(audit["warnings"], list), f"table audit ran ({len(audit['errors'])} errors, {len(audit['warnings'])} warnings)")
    t.call("vpx_script", {"action": "append", "text": "Sub SmokeDup()\nEnd Sub\nSub SmokeDup()\nEnd Sub\n"})
    audit = t.call("vpx_table", {"action": "audit"})
    check(any("smokedup" in e.lower() for e in audit["errors"]), f"audit reports a duplicate Sub in the script: {audit}")
    t.undo()
    check(not any("smokedup" in e.lower() for e in t.call("vpx_table", {"action": "audit"})["errors"]), "undo removed the appended script text")
    script_path = os.path.join(t.workdir, "smoke_script.vbs")
    t.call("vpx_script", {"action": "export", "path": script_path})
    with open(script_path, "a") as f:
        f.write("\nSub SmokeFromFile()\nEnd Sub\n")
    t.call("vpx_script", {"action": "set", "path": script_path})
    check(t.call("vpx_script", {"action": "get", "sub": "SmokeFromFile"})["text"].startswith("Sub SmokeFromFile"), "script exported to a file and set back from it")


def step_drawing_order(t):
    decal = t.call("vpx_part", {"action": "add", "type": "Decal", "x": 300, "y": 1200, "name": "SmokeDecal", "data": {"type": 0, "text": "VPX", "width": 200, "height": 80}})
    check(decal["name"] == "SmokeDecal", "text decal added")
    t.render()
    names_before = t.part_names()
    order = t.call("vpx_part", {"action": "order", "names": ["SmokeDecal", "Wall25"]})["order"]
    names = t.part_names()
    check(order == ["SmokeDecal", "Wall25"] and names[-2:] == ["SmokeDecal", "Wall25"], f"drawing order set: {names[-2:]}")
    t.undo()
    check(t.part_names() == names_before, "undo restored the drawing order")
    t.call("vpx_part", {"action": "delete", "names": ["SmokeDecal"]})


def step_table_properties(t):
    tbl = t.call("vpx_table", {"action": "get"})
    check("image" in tbl["data"] and "gravity" in tbl["data"] and "meaning" not in tbl, f"table properties read ({len(tbl['data'])} fields)")
    old_slope = tbl["data"]["slope_min"]
    r = t.call("vpx_table", {"action": "set", "data": {"slope_min": old_slope + 1, "Gravity": tbl["data"]["gravity"]}})
    check(t.call("vpx_table", {"action": "get", "fields": ["slope_min"]})["data"]["slope_min"] == old_slope + 1 and "gravity" in r["applied"], "table property set, script-style name mapped")
    t.undo()
    check(t.call("vpx_table", {"action": "get", "fields": ["slope_min"]})["data"]["slope_min"] == old_slope, "undo restored the table property")
    expect_error(lambda: t.call("vpx_table", {"action": "set", "data": {"image": "NoSuchImage"}}), "unknown playfield image refused")


def step_geometry_queries(t):
    fl = t.field("LeftFlipper", "center")
    at = t.call("vpx_geometry", {"action": "at", "x": fl["x"], "y": fl["y"]})
    check(any(p["name"] == "LeftFlipper" for p in at["parts"]), "hit test at a point finds the flipper")
    gap = t.call("vpx_geometry", {"action": "gap", "names": ["LeftFlipper", "RightFlipper"]})
    check(20 < gap["gap"] < 200, f"gap between the flippers measured: {gap['gap']:.1f} (ball fits: {gap['ballFits']})")
    guide = t.call("vpx_part", {"action": "add", "type": "Wall", "x": 0, "y": 0, "name": "SmokeGuide", "centerline": [{"x": 300, "y": 900}, {"x": 400, "y": 1000}, {"x": 400, "y": 1150}], "thickness": 8})
    check(len(guide["points"]) == 6, "wall built from a centerline and a thickness")
    guide2 = t.call("vpx_part", {"action": "add", "type": "Wall", "x": 0, "y": 0, "name": "SmokeGuide2", "centerline": [{"x": 380, "y": 1000}, {"x": 420, "y": 1000}], "thickness": 8})
    check("SmokeGuide" in guide2.get("overlaps", []), "overlapping part reported on add")
    ins = t.call("vpx_part", {"action": "add", "type": "Light", "x": 600, "y": 900, "name": "SmokeInsert", "circle": 22, "segments": 12, "data": {"shape": 1}})
    check(len(ins["points"]) == 12, "round insert outline generated")
    t.call("vpx_part", {"action": "delete", "names": ["SmokeGuide", "SmokeGuide2", "SmokeInsert"]})
    top_img = t.call("vpx_screenshot", {"view": "top", "maxSize": 300})
    check(isinstance(top_img, str) and "screenshot" in top_img.lower(), f"top view captured: {top_img}")
    check(t.status()["editorOpened"], "editor view restored after the top capture")


def step_generated_meshes(t):
    disc = t.call("vpx_part", {"action": "add", "type": "Primitive", "x": 450, "y": 1000, "name": "SmokeDisc", "data": {"image": t.first_image}})
    check(disc["name"] == "SmokeDisc", "primitive for a disc added")
    m = t.call("vpx_part", {"action": "mesh", "name": "SmokeDisc", "shape": "disc", "radius": 60, "height": 6, "segments": 24})
    check(m["vertices"] == 1 + 24 + 1 + 24 + 50 and m["triangles"] == 24 * 4 and m["bounds"]["max"]["z"] == 6, f"disc mesh generated ({m['vertices']} vertices, {m['triangles']} triangles)")
    got = t.call("vpx_part", {"action": "get", "name": "SmokeDisc", "fields": ["use_mesh", "size"]})
    check(got["data"]["use_mesh"] is True and got["data"]["size"]["x"] == 1 and got["mesh"]["meshFile"] == "generated:disc", "primitive reports its mesh")
    t.render()
    m = t.call("vpx_part", {"action": "mesh", "name": "SmokeDisc", "shape": "box", "width": 100, "depth": 60, "height": 20})
    check(m["triangles"] == 12 and m["bounds"]["min"]["x"] == -50, "box mesh generated")
    t.undo()
    got = t.call("vpx_part", {"action": "get", "name": "SmokeDisc"})
    check(got["mesh"]["meshFile"] == "generated:disc" and got["mesh"]["triangles"] == 96 and got["data"]["size"]["x"] == 1, "undo restored the previous mesh")
    t.undo()
    got = t.call("vpx_part", {"action": "get", "name": "SmokeDisc"})
    check(got["mesh"].get("builtin") is True, "undo restored the built-in shape")
    t.render()
    expect_error(lambda: t.call("vpx_part", {"action": "mesh", "name": "SmokeDisc", "shape": "sphere"}), "unknown shape is refused")
    t.call("vpx_part", {"action": "delete", "names": ["SmokeDisc"]})


def step_single_part_transform(t):
    pts_before = t.call("vpx_part", {"action": "points", "name": "Wall25"})["points"]
    r = t.call("vpx_part", {"action": "transform", "name": "Wall25", "dx": 50, "rotate": 15, "scale": 1.5})
    check(r["done"] == ["rotate", "scale", "translate"], "wall transformed: rotate, scale, translate")
    t.undo()
    pts_after = t.call("vpx_part", {"action": "points", "name": "Wall25"})["points"]
    check(abs(pts_after[0]["x"] - pts_before[0]["x"]) < 0.01, "undo restored the wall's points")

    def flipper_geom():
        d = t.fields("LeftFlipper", "center", "start_angle", "end_angle", "flipper_radius_max")
        return d["center"]["x"], d["start_angle"], d["end_angle"], d["flipper_radius_max"]

    fl = flipper_geom()
    r = t.call("vpx_part", {"action": "transform", "name": "LeftFlipper", "dx": 10, "rotate": 5, "scale": 1.2})
    got = flipper_geom()
    check(abs(got[0] - fl[0] - 10) < 0.01 and abs(got[1] - fl[1] - 5) < 0.01 and abs(got[2] - fl[2] - 5) < 0.01 and abs(got[3] - fl[3] * 1.2) < 0.01, f"flipper transformed through its fields: {r['done']}")
    t.undo()
    got = flipper_geom()
    check(abs(got[0] - fl[0]) < 0.01 and abs(got[3] - fl[3]) < 0.01, "undo restored the flipper")


def step_images(t):
    images = t.call("vpx_image", {"action": "list"})
    check(images["count"] > 0 and all("width" in i for i in images["images"]), f"{images['count']} images listed")
    first = images["images"][0]["name"]
    got = t.call("vpx_image", {"action": "get", "name": first, "exportPath": t.export_path})
    check(got.get("exported") == t.export_path and os.path.getsize(t.export_path) > 100, f"image '{first}' exported to PNG")
    added = t.call("vpx_image", {"action": "add", "path": t.export_path, "name": "SmokeImage"})
    check(added["name"] == "SmokeImage" and added["width"] == images["images"][0]["width"], "image imported back under a new name")
    t.call("vpx_part", {"action": "set", "name": "Peg1", "data": {"image": "SmokeImage"}})
    t.render()
    t.call("vpx_part", {"action": "set", "name": "Peg1", "data": {"image": ""}})
    t.call("vpx_image", {"action": "delete", "name": "SmokeImage"})
    check(t.call("vpx_image", {"action": "list"})["count"] == images["count"], "image deleted")
    t.undo()
    check(t.call("vpx_image", {"action": "get", "name": "SmokeImage"})["width"] == added["width"], "undo restored the deleted image")
    t.undo(3)
    check(t.call("vpx_image", {"action": "list"})["count"] == images["count"], "undo removed the imported image")


def step_sounds(t):
    sounds = t.call("vpx_sound", {"action": "list"})
    if sounds["count"] == 0:
        return
    snd = sounds["sounds"][0]
    got = t.call("vpx_sound", {"action": "set", "name": snd["name"], "volume": -20})
    check(got["volume"] == -20, f"sound '{snd['name']}' volume set")
    t.undo()
    check(next(s["volume"] for s in t.call("vpx_sound", {"action": "list"})["sounds"] if s["name"] == snd["name"]) == snd["volume"], "undo restored the sound volume")


def step_collections(t):
    def find(name):
        return next((c for c in t.call("vpx_collection", {"action": "list"})["collections"] if c["name"] == name), None)

    coll = t.call("vpx_collection", {"action": "create", "name": "SmokeColl", "parts": ["LeftFlipper", "RightFlipper", "Nope"], "fireEvents": True})
    check(coll["parts"] == ["LeftFlipper", "RightFlipper"] and coll["notFound"] == ["Nope"] and coll["fireEvents"], "collection created with members and flags")
    coll = t.call("vpx_collection", {"action": "remove", "name": "SmokeColl", "parts": ["RightFlipper"]})
    check(coll["parts"] == ["LeftFlipper"], "member removed from the collection")
    t.call("vpx_collection", {"action": "delete", "name": "SmokeColl"})
    check(find("SmokeColl") is None, "collection deleted")
    t.undo()
    coll = find("SmokeColl")
    check(coll and coll["parts"] == ["LeftFlipper"] and coll["fireEvents"], "undo restored the deleted collection with its members")
    t.undo()
    check(find("SmokeColl")["parts"] == ["LeftFlipper", "RightFlipper"], "undo restored the removed member")
    t.undo()
    check(find("SmokeColl") is None, "undo removed the created collection")


def step_materials(t):
    def material(name):
        return t.call("vpx_material", {"action": "get", "name": name})["data"]

    n_materials = t.call("vpx_material", {"action": "list"})["count"]
    white = material("Plastic White")
    check(white["base_color"] != 0 and "roughness" in white, f"material fields read ({len(white)} fields)")
    added = t.call("vpx_material", {"action": "add", "name": "SmokeRed", "copyOf": "Plastic White", "data": {"base_color": 255, "roughness": 0.9}})["data"]
    check(added["base_color"] == 255 and abs(added["roughness"] - 0.9) < 0.01, "material added from a copy with overrides")
    t.call("vpx_material", {"action": "set", "name": "SmokeRed", "data": {"roughness": 0.2}})
    got = material("SmokeRed")
    check(got["base_color"] == 255 and abs(got["roughness"] - 0.2) < 0.01, "material set changed one field and kept the others")
    peg_mat = t.field("Peg1", "material")
    t.call("vpx_part", {"action": "set", "name": "Peg1", "data": {"material": "SmokeRed"}})
    t.render()
    t.call("vpx_part", {"action": "set", "name": "Peg1", "data": {"material": peg_mat}})
    t.call("vpx_material", {"action": "delete", "name": "SmokeRed"})
    check(t.call("vpx_material", {"action": "list"})["count"] == n_materials, "material deleted")
    expect_error(lambda: material("SmokeRed"), "deleted material is not found")
    t.undo()
    check(abs(material("SmokeRed")["roughness"] - 0.2) < 0.01 and t.call("vpx_material", {"action": "list"})["count"] == n_materials + 1, "undo restored the deleted material")
    t.undo(3)
    check(abs(material("SmokeRed")["roughness"] - 0.9) < 0.01, "undo restored the material field")
    t.undo()
    check(t.call("vpx_material", {"action": "list"})["count"] == n_materials, "undo removed the added material")


def step_donor_library(t):
    t.donor = find_asset(t.executable, "exampleTable.vpz", [os.path.expanduser("~/vpx/vpx-editor/public/templates/exampleTable.vpx")])
    info = t.call("vpx_library", {"action": "inspect", "path": t.donor, "type": "Bumper"})
    t.donor_names = [p["name"] for p in info["parts"]]
    check(len(t.donor_names) >= 4, f"donor table {os.path.basename(t.donor)} has bumpers {t.donor_names}")
    sub = t.call("vpx_library", {"action": "get_script", "path": t.donor, "sub": f"{t.donor_names[0]}_Hit"})
    check(sub["text"].lower().startswith(f"sub {t.donor_names[0]}_hit".lower()), "donor script Sub read without loading the table")

    groups_before = t.count("PartGroup")
    g = t.call("vpx_part", {"action": "add", "type": "PartGroup", "name": "SmokeGroup"})
    check(g["type"] == "PartGroup" and g["name"] == "SmokeGroup", "part group added")
    r = t.call("vpx_library", {"action": "clone", "path": t.donor, "parts": t.donor_names[:2], "withScript": False, "group": "SmokeGroup"})
    in_group = [p for p in r["imported"] if p["type"] == "Bumper" and p.get("group") == "SmokeGroup"]
    check(len(in_group) == 2 and r["group"] == "SmokeGroup" and all(p["type"] != "PartGroup" or p["name"] == "SmokeGroup" for p in r["imported"]), "clone placed the parts in the requested group without a new root group")
    check(t.count("PartGroup") == groups_before + 1, "no numbered group piled up")
    r = t.call("vpx_part", {"action": "delete", "names": ["SmokeGroup"]})
    check(r["deleted"] == 0 and "withChildren" in r["notDeleted"][0]["reason"], "deleting a non-empty group is refused with the reason")
    r = t.call("vpx_part", {"action": "delete", "names": ["SmokeGroup"], "withChildren": True})
    check(r["deleted"] >= 3, f"group deleted with its {r['deleted'] - 1} parts")
    check(t.count("PartGroup") == groups_before and t.count("Bumper") == t.bumpers_at_start, "table back to its part count")


def step_clone_bumpers(t):
    """Three complete bumpers wired to our own handlers, then a fourth carrying the donor's handlers (three undo records)."""
    t.cloned = t.donor_names[:3]
    r = t.call("vpx_library", {"action": "clone", "path": t.donor, "parts": t.cloned, "withScript": False})
    cloned = [p["name"] for p in r["imported"] if p["type"] == "Bumper"]
    check(cloned == t.cloned, f"cloned {cloned} with their materials")
    got = t.fields(t.cloned[0], "material", "center")
    check(got["material"] != "", "cloned bumper has a cap material")
    cap = t.call("vpx_material", {"action": "get", "name": got["material"]})["data"]
    check("base_color" in cap and cap["base_color"] != 0, f"cloned material '{got['material']}' came with its values (base_color {cap['base_color']})")
    t.target = (got["center"]["x"], got["center"]["y"])
    script = "Dim SmokeHits : SmokeHits = 0\n" + "".join(f"Sub {n}_Hit() : SmokeHits = SmokeHits + 1 : End Sub\n" for n in t.cloned)
    t.call("vpx_script", {"action": "append", "text": script})
    check(t.call("vpx_script", {"action": "get", "sub": f"{t.cloned[0]}_Hit"})["text"].startswith(f"Sub {t.cloned[0]}_Hit"), "hit handlers appended to the script")

    r = t.call("vpx_library", {"action": "clone", "path": t.donor, "parts": [t.donor_names[3]]})
    copied = [c.lower() for c in r.get("copiedSubs", [])]
    check(f"{t.donor_names[3]}_hit".lower() in copied, f"clone copied the {t.donor_names[3]}_Hit handler from the donor script")
    check(len(copied) > 1, f"clone also copied the helpers the handler calls: {copied[1:6]}")


def step_playtest_start(t):
    status = t.call("vpx_editor", {"action": "playtest"})
    check(status["isLiveCopy"] and "switched" in status, "playtest returned once the play session runs")
    status = t.wait_status(lambda s: s["isLiveCopy"] and s["playing"], 30, "playtest session")
    check(len(status["recentScriptErrors"]) == 0, "script compiled without errors")


def step_sim_shot_and_plunge(t):
    shot = t.call("vpx_sim", t.shot_at_target(speed=40, ms=2500, sampleMs=100), timeout=600)
    hits = [e for e in shot["events"] if e["event"] == "Hit" and e["part"] in t.cloned]
    zs = [s["z"] for s in shot["samples"]]
    check(min(zs) > 0, f"ball stayed above the playfield (min z {min(zs):.1f})")
    check(len(hits) > 0, f"bumpers were hit: {[h['part'] for h in hits]}")
    check(t.call("vpx_script", {"action": "eval", "text": "SmokeHits"})["value"] >= 1, "script hit handler ran")
    check(all("ball" in e for e in shot["events"] if e["event"] in ("Hit", "Unhit")) and "otherBallEvents" in shot, "shot events carry the ball id and other balls' events are counted apart")
    status = t.status()
    check(status["mcpEditsSinceSave"] > 0 and "uptimeMinutes" in status, f"status counts {status['mcpEditsSinceSave']} MCP edits since the last save")
    expect_error(lambda: t.call("vpx_image", {"action": "add", "path": t.export_path, "name": "SmokeLost"}), "asset edits are refused during a playtest", "playtest")

    plunge = t.call("vpx_sim", {"action": "plunge", "pull": 1, "holdMs": 400, "ms": 4000, "sampleMs": 100}, timeout=600)
    travelled = [b for b in plunge["balls"] if b["samples"] and min(s["y"] for s in b["samples"]) < 1000]
    check(len(travelled) > 0, f"full plunge sent a ball up the table ({len(plunge['events'])} events)")
    check(all(v > 0.9 for v in plunge.get("pullAtRelease", {}).values()) and plunge.get("pullAtRelease"), f"plunge reports the pull at release: {plunge.get('pullAtRelease')}")
    initial = t.call("vpx_ball", {"action": "control"})
    ctl = t.call("vpx_ball", {"action": "control", "throwBalls": True, "ballControl": False, "size": 40})
    check(ctl["mode"] == "throwNewBall" and ctl["throwBalls"] and not ctl["ballControl"] and ctl["size"] == 40, "throw balls mode switched on for the mouse")
    ctl = t.call("vpx_ball", {"action": "control", "ballControl": True})
    check(ctl["mode"] == "throwDraggedBall", "ball control added on top keeps throw balls")
    ctl = t.call("vpx_ball", {"action": "control", "ballControl": False, "throwBalls": False})
    check(ctl["mode"] == "disabled" and t.call("vpx_ball", {"action": "control"})["mode"] == "disabled", "mouse ball modes switched off and reported")
    t.call("vpx_ball", {"action": "control", "ballControl": initial["ballControl"], "throwBalls": initial["throwBalls"], "size": initial["size"], "mass": initial["mass"]})


def step_sim_flips(t):
    sweep = t.call("vpx_sim", {"action": "sweep", "over": "flipWhenAlong", "values": [0.2, 0.6], "base": {"action": "flip", "x": 130, "y": 1450, "vy": 4, "ms": 3000, "sampleMs": 100}}, timeout=1800)
    check(len(sweep["runs"]) == 2 and all("outcome" in r and "topY" in r for r in sweep["runs"]), f"sweep over the release point: {[(r['value'], r['outcome'], round(r['topY'])) for r in sweep['runs']]}")
    flip = t.call("vpx_sim", {"action": "flip", "x": 130, "y": 1450, "vy": 4, "flipWhenAlong": 0.6, "ms": 5000, "sampleMs": 100}, timeout=600)
    top = min(s["y"] for s in flip["samples"])
    check(flip["triggered"] and 0.5 < flip["atFlip"]["along"] < 0.8, f"flip triggered at {flip['atFlip']['along']:.2f} along the {flip['flipper']['name']}")
    check("exit" in flip and flip["exit"]["speed"] > 5, f"exit vector measured: {flip['exit']['angle']:.0f} deg at {flip['exit']['speed']:.1f}, top y {top:.0f}, outcome: {flip['outcome']}")
    until = {"approachingFlipper": True, "event": "Drain", "ballLost": True}
    t.call("vpx_sim", {"action": "trace", "x": 0, "ms": 6000, "sampleMs": 100, "until": until}, timeout=600)
    t.call("vpx_ball", {"action": "create", "x": 743, "y": 1450, "vy": 4})
    approach = t.call("vpx_sim", {"action": "trace", "ms": 6000, "sampleMs": 100, "until": until}, timeout=600)
    check(approach["stoppedBy"] == "approachingFlipper" and approach["approach"]["side"] == "right", f"trace stopped when a ball approached the {approach.get('approach', {}).get('flipper')} at along {approach.get('approach', {}).get('along', 0):.2f}")
    t.call("vpx_ball", {"action": "destroy", "id": approach["approach"]["ballId"]})


def step_script_errors(t):
    before_seq = t.status()["lastScriptErrorSeq"]
    text = expect_error(lambda: t.call("vpx_script", {"action": "run", "text": "smokeZero = 1 / 0"}), "a failing script run is reported as an error")
    r = json.loads(text.split(": ", 1)[1])
    check(r["errors"] and r["errors"][0]["seq"] > before_seq, "script error reported with a new sequence number")
    st = t.status()
    check(st["scriptErrors"] >= 1 and st["recentScriptErrors"][-1]["description"] == r["errors"][0]["description"] and "recentLogErrors" in st, "status lists the error of this session")
    errs = t.call("vpx_sim", {"action": "shot", "x": 300, "y": 1300, "ms": 300}, timeout=600)["scriptErrors"]
    check(isinstance(errs, dict) and "count" in errs and "distinct" in errs, "sim results summarize script errors")


def step_sim_sweeps_and_tracking(t):
    kept = t.call("vpx_sim", {"action": "shot", "x": 300, "y": 1300, "vx": 0, "vy": 0, "ms": 1500, "keepBall": True}, timeout=600)
    check(kept.get("ballId") is not None and kept.get("state") in ("settled", "moving") and "touching" in kept, f"shot kept its ball and reports its state ({kept.get('state')}, touching {kept.get('touching')})")
    t.call("vpx_ball", {"action": "destroy", "id": kept["ballId"]})
    base = {"action": "flip", "x": 130, "y": 1450, "ms": 1500, "sampleMs": 100}
    sweep = t.call("vpx_sim", {"action": "sweep", "over": ["flipWhenAlong", "vy"], "values": [[0.3, 0.6], [4]], "base": base}, timeout=1800)
    check(len(sweep["runs"]) == 2 and isinstance(sweep["runs"][0]["value"], dict), "sweep over two parameters ran every combination")
    sweep = t.call("vpx_sim", {"action": "sweep", "over": '["flipWhenAlong", "vy"]', "values": "[[0.3], [4]]", "base": dict(base, ms=1000)}, timeout=1800)
    check(len(sweep["runs"]) == 1 and "error" not in sweep["runs"][0], "sweep accepts over and values sent as JSON strings")
    sweep = t.call("vpx_sim", {"action": "sweep", "over": ["x", "y"], "points": [[130, 1450], [140, 1440]], "base": {"action": "flip", "vy": 4, "ms": 1000, "sampleMs": 100}}, timeout=1800)
    check(len(sweep["runs"]) == 2 and all("error" not in r for r in sweep["runs"]) and sweep["runs"][1]["value"] == {"x": 140, "y": 1440}, "sweep over explicit x, y points")
    tracked = t.call("vpx_sim", t.shot_at_target(speed=40, goal="Bumper1", crossAt={"y": 1200}), timeout=600)
    check(tracked["rode"] and tracked["rode"][0]["on"] == "playfield" and "Bumper1" in tracked["contacts"] and tracked["goal"]["made"] and tracked["crossings"], f"shot reports what it rode ({[s['on'] for s in tracked['rode']]}), its goal and the y=1200 crossing")
    t.call("vpx_sim", {"action": "shot", "x": 300, "y": 1300, "ms": 200, "keepBall": True}, timeout=600)
    iso = t.call("vpx_sim", {"action": "sweep", "over": "speed", "values": [30, 40], "goal": "Bumper1", "isolate": True, "base": t.shot_at_target()}, timeout=1800)
    check(iso["goalRate"]["runs"] == 2 and all("goal" in r and "rode" in r and "isolated" in r for r in iso["runs"]) and iso["runs"][0]["isolated"]["removedBefore"], f"isolated sweep cleared the kept ball and reports goal rate {iso['goalRate']['percent']}%")
    check(len(t.call("vpx_ball", {"action": "list"})["balls"]) == 0, "isolated runs left no balls behind")


def step_background_jobs(t):
    started = t.call("vpx_sim", {"action": "sweep", "over": "speed", "values": [30, 40], "goal": "Bumper1", "isolate": True, "base": t.shot_at_target(), "background": True}, timeout=30)
    check(started["state"] == "running" and started["job"].startswith("job-"), f"background sweep returned {started['job']} at once")
    done = t.call("vpx_job", {"action": "get", "id": started["job"], "waitMs": 120000}, timeout=200)
    check(done["job"]["state"] == "done" and len(done["runs"]) == 2 and done["goalRate"]["runs"] == 2, "vpx_job get waited for the sweep and returned its result")
    check(any(j["id"] == started["job"] for j in t.call("vpx_job", {"action": "list"})["jobs"]), "vpx_job list keeps finished jobs")
    soak = t.call("vpx_sim", {"action": "autoplay", "ms": 120000, "background": True}, timeout=30)
    time.sleep(2)
    running = t.call("vpx_job", {"action": "get", "id": soak["job"]})
    check(running["state"] == "running" and "game seconds" in running.get("progress", ""), f"background autoplay reports progress {running.get('progress')}")
    check(t.call("vpx_job", {"action": "cancel", "id": soak["job"]})["state"] == "cancelled", "background job cancelled")
    time.sleep(1)
    check(not t.status()["playing"], "a cancelled run leaves the table paused")
    t.call("vpx_ball", {"action": "destroy"})


def step_scenarios(t):
    shot = t.shot_at_target()
    scenario = {"name": "smoke shots", "defaults": {"ms": 1500, "isolate": True}, "runs": [
        {"name": "bumper", "args": dict(shot, speed=40, goal="Bumper1"), "expect": {"goal": True, "fired": ["Bumper1.Hit"]}},
        {"name": "bumper missed", "args": dict(shot, speed=40, goal="Bumper1"), "expect": {"goal": False}},
        {"name": "bumper sweep", "action": "sweep", "over": "speed", "values": [30, 40], "base": shot, "goal": "Bumper1", "expect": {"goalRate": 50}}]}
    res = t.call("vpx_sim", {"action": "scenario", "scenario": scenario}, timeout=600)
    check(res["passed"] == 2 and res["failed"] == 1 and not res["runs"][1]["passed"] and "goal" in res["runs"][1]["failed"][0], f"inline scenario: {res['passed']} passed, the deliberate miss failed: {res['runs'][1].get('failed')}")
    folder = os.path.dirname(t.status()["tablePath"]) or t.workdir
    with open(os.path.join(folder, "smoke_scenario.json"), "w") as f:
        json.dump(scenario, f)
    res = t.call("vpx_sim", {"action": "scenario", "path": "smoke_scenario.json", "only": ["bumper"], "save": "smoke_results.json"}, timeout=600)
    check(res["passed"] == 1 and res["failed"] == 0 and os.path.isfile(os.path.join(folder, "smoke_results.json")), "scenario file relative to the table, only and save")


def step_captures_in_play(t):
    top_play = t.call("vpx_screenshot", {"view": "top", "maxSize": 300})
    check(isinstance(top_play, str) and "screenshot" in top_play.lower(), "top view captured during a playtest with the last trajectory")
    status = t.status()
    check(status["isLiveCopy"] and not status["editorOpened"], "playtest left as it was after the top capture")
    tt = t.client.rpc("tools/call", {"name": "vpx_screenshot", "arguments": {"turntable": {"count": 3}, "maxSize": 200}}, timeout=120)["result"]
    check(not tt["isError"] and sum(1 for c in tt["content"] if c["type"] == "image") == 3 and len(json.loads(tt["content"][0]["text"])["turntable"]) == 3, "turntable returned 3 views")
    t.call("vpx_script", {"action": "run", "text": "DMDWidth = 16 : DMDHeight = 4 : Dim smokePx(63) : For smokeI = 0 To 63 : smokePx(smokeI) = (smokeI Mod 2) * 100 : Next : DMDPixels = smokePx"})
    t.call("vpx_play", {"ms": 300})
    dmd = t.call("vpx_screenshot", {"target": "dmd"})
    check(isinstance(dmd, str) and "16x4" in dmd, f"script DMD frame captured: {dmd}")


def step_stuckscan_and_autoplay(t):
    balls_before = len(t.call("vpx_ball", {"action": "list"})["balls"])
    scan = t.call("vpx_sim", {"action": "stuckscan", "area": {"left": 330, "top": 850, "right": 620, "bottom": 1000}, "step": 70, "ms": 2000, "maxBalls": max(1, 5 - balls_before)}, timeout=900)
    check(scan["tested"] > 0 and "stuckSpots" in scan and len(t.call("vpx_ball", {"action": "list"})["balls"]) <= balls_before, f"stuck scan ran ({scan['tested']} balls, {len(scan['stuckSpots'])} resting spots) and cleaned up")
    auto = t.call("vpx_sim", {"action": "autoplay", "ms": 8000}, timeout=900)
    check(auto["playedMs"] >= 7900 and "switchesNeverFired" in auto and "eventCounts" in auto, f"autoplay soak ran: {auto['plunges']} plunges, {auto['flips']} flips, {auto['ballsLost']} balls lost")


def step_playtest_stop(t):
    status = t.call("vpx_editor", {"action": "stop"})
    check(not status["isLiveCopy"] and status["editorOpened"] and "switched" in status, "stop returned once the editor was restored")
    check(t.count("Bumper") == t.bumpers_at_start + 4, "bumpers survived the playtest")


def step_editor_views(t):
    bp = t.call("vpx_screenshot", {"view": "blueprint", "maxSize": 300})
    check(isinstance(bp, str) and "blueprint" in bp, "blueprint schematic captured")
    reg = t.call("vpx_screenshot", {"view": "top", "region": {"left": 300, "top": 800, "right": 700, "bottom": 1000}, "maxSize": 300})
    check(isinstance(reg, str) and "region 300,800" in reg, "top view cropped to a region")
    bpz = t.call("vpx_screenshot", {"view": "blueprint", "zoom": 2, "center": [475, 900], "maxSize": 300})
    check(isinstance(bpz, str) and "zoom" in bpz, "blueprint zoomed around a point")
    lay = t.call("vpx_editor", {"action": "layout", "layout": "blueprint"})
    check(lay["layout"] == "blueprint" and any(a["type"] == "Blueprint" for a in lay["areas"]), "blueprint layout preset applied")
    lay = t.call("vpx_editor", {"action": "show", "area": "script"})
    check(any(a["type"] == "Script" and not a["hidden"] for a in lay["areas"]), "script area shown")
    lay = t.call("vpx_editor", {"action": "layout", "layout": "layout", "reset": True})
    types = sorted(a["type"] for a in lay["areas"])
    check(lay["layout"] == "layout" and types == ["Outliner", "Properties", "Viewport", "ViewportPreview"] and not lay["maximized"] and not lay["flyMode"], f"layout reset to its default areas {types}")
    two = t.donor_names[:2]
    sel = t.call("vpx_editor", {"action": "select", "names": two + ["NoSuchPart"]})
    check(sel["selection"] == two and sel["notFound"] == ["NoSuchPart"], "parts selected, unknown names reported")
    check(t.status()["selection"] == two, "status reports the selection")
    fr = t.call("vpx_editor", {"action": "frame"})
    check(fr["selection"] == two, "views framed on the selection")


def step_group_transform_undo_redo(t):
    a, b = t.donor_names[:2]
    c0, c1 = t.field(a, "center"), t.field(b, "center")
    tr = t.call("vpx_part", {"action": "transform", "names": [a, b], "rotate": 180})
    swapped = tr["centers"][a]
    check(abs(swapped["x"] - c1["x"]) < 0.5 and abs(swapped["y"] - c1["y"]) < 0.5 and tr["done"] == ["rotate"], "group rotation about the selection center swapped the two bumpers")
    un = t.undo()
    back = t.field(a, "center")
    check(abs(back["x"] - c0["x"]) < 0.5 and abs(back["y"] - c0["y"]) < 0.5, "undo restored the group transform")
    check(un["undone"].startswith("MCP transform") and un["canRedo"] and un["nextRedo"] == un["undone"], f"undo reports the history label ({un['undone']}) and the redo step")
    re_ = t.redo()
    again = t.field(a, "center")
    check(abs(again["x"] - c1["x"]) < 0.5 and re_["redone"] == un["undone"] and not re_["canRedo"], "redo reapplied the group transform")
    t.undo()
    st = t.status()
    check(st["canUndo"] and st["canRedo"] and st["nextRedo"].startswith("MCP transform"), "status reports the undo and redo steps")

    def material(name):
        return t.call("vpx_material", {"action": "get", "name": name})["data"]

    mat_name = t.call("vpx_material", {"action": "list"})["materials"][0]["name"]
    rough = material(mat_name)["roughness"]
    t.call("vpx_material", {"action": "set", "name": mat_name, "data": {"roughness": 0.123}})
    un = t.undo()
    check(un["undone"] == f"MCP material: {mat_name}" and material(mat_name)["roughness"] == rough, "material edit undone with its label")
    t.redo()
    check(abs(material(mat_name)["roughness"] - 0.123) < 1e-4, "material edit redone")
    t.undo()
    t.call("vpx_material", {"action": "add", "name": "SmokeRedoMat", "data": {"roughness": 0.5}})
    t.undo()
    check(all(m["name"] != "SmokeRedoMat" for m in t.call("vpx_material", {"action": "list"})["materials"]), "material add undone")
    t.redo()
    check(material("SmokeRedoMat")["roughness"] == 0.5, "material add redone")
    t.undo()


def step_selection_edits(t):
    a = t.donor_names[0]
    c0 = t.field(a, "center")
    dup = t.call("vpx_editor", {"action": "duplicate", "names": [a], "offset": [100, 0]})
    check(dup["duplicated"] == [a] and len(dup["parts"]) == 1 and dup["parts"][0] != a, f"bumper duplicated as {dup['parts']}")
    dc = t.field(dup["parts"][0], "center")
    check(abs(dc["x"] - c0["x"] - 100) < 0.5 and abs(dc["y"] - c0["y"]) < 0.5, "the copy sits at the offset")
    check(t.status()["selection"] == dup["parts"], "the copy is selected")
    un = t.undo()
    check(un["undone"].startswith("Duplicate") and t.count("Bumper") == t.bumpers_at_start + 4, "undo removed the copy in one step")

    prims_before = t.count("Primitive")
    t.call("vpx_part", {"action": "add", "type": "Primitive", "x": 400, "y": 1200, "name": "SmokeOrigin"})
    t.call("vpx_part", {"action": "mesh", "name": "SmokeOrigin", "shape": "box", "width": 50, "depth": 50, "height": 40})
    pos0 = t.field("SmokeOrigin", "position")
    t.call("vpx_part", {"action": "set", "name": "SmokeOrigin", "data": {"position": {"x": pos0["x"], "y": pos0["y"], "z": 30}}})
    so = t.call("vpx_editor", {"action": "set_origin", "names": ["SmokeOrigin"], "mode": "geometry"})
    check(so["mode"] == "geometry" and so["parts"][0]["name"] == "SmokeOrigin" and abs(so["parts"][0]["position"][2] - 50) < 0.5, f"set origin moved the pivot to the box center ({so['parts'][0]['position']})")
    bounds = t.call("vpx_part", {"action": "get", "name": "SmokeOrigin"})["bounds"]
    check(abs(bounds["min"][2] - 30) < 0.5 and abs(bounds["max"][2] - 70) < 0.5, f"the geometry did not move ({bounds['min'][2]}..{bounds['max'][2]})")
    un = t.undo()
    check(un["undone"].startswith("Set Origin") and abs(t.field("SmokeOrigin", "position")["z"] - 30) < 0.5, "set origin undone")
    t.undo(3)
    check(t.count("Primitive") == prims_before, "origin primitive removed")

    total = t.count()
    sel = t.call("vpx_editor", {"action": "select", "all": True})
    n_all = len(sel["selection"])
    check(a in sel["selection"] and n_all <= total, f"select all selected the {n_all} pickable parts of {total}")
    t.call("vpx_editor", {"action": "select", "names": [a]})
    sel = t.call("vpx_editor", {"action": "select", "invert": True})
    check(len(sel["selection"]) == n_all - 1 and a not in sel["selection"], "invert selection")
    fr = t.call("vpx_editor", {"action": "frame", "all": True})
    check("layout" in fr, "views framed on the whole table")
    t.call("vpx_editor", {"action": "select", "names": []})
    lights = t.count("Light")
    if lights:
        li = t.call("vpx_editor", {"action": "lights", "state": 1})
        check(li["lights"] == lights and li["state"] == 1, f"{lights} lights switched on for the editor view")
    t.call("vpx_editor", {"action": "layout", "layout": "layout"})


def step_undo_clones(t):
    """The three records of step_clone_bumpers: the second clone with its donor handlers, the appended handlers, the first clone."""
    t.undo()
    check(t.count("Bumper") == t.bumpers_at_start + 3, "undo removed the second clone")
    expect_error(lambda: t.call("vpx_script", {"action": "get", "sub": f"{t.donor_names[3]}_Hit"}), "the copied donor handlers went with the clone")
    t.undo(2)
    check(t.count("Bumper") == t.bumpers_at_start, "undo removed the cloned bumpers")
    expect_error(lambda: t.call("vpx_script", {"action": "get", "sub": f"{t.cloned[0]}_Hit"}), "undo removed the appended hit handlers")


def step_reopen_table(t):
    reopened = t.call("vpx_table", {"action": "open", "path": t.table}, timeout=180)
    check("switched" in reopened and reopened["editorOpened"] and reopened["playMode"] == "FullEdit", "table reopened, the new editor session is up")


def step_export_package(t):
    """One folder, exactly one table (the iOS import refuses archives with several), no cache."""
    def touch(*parts):
        path = os.path.join(t.workdir, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("x")

    shutil.copytree(t.table, os.path.join(t.workdir, "Smoke old.vpz"))
    touch("cache", "junk.bin")
    touch(".DS_Store")
    touch("art", "layer.psd")
    out = os.path.join(t.workdir, "pkg", "smoke.vpxz")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    exp = t.call("vpx_export", {"path": out}, timeout=600)
    with zipfile.ZipFile(out) as z:
        entries = z.namelist()
    tables = sorted({n.split("/")[1] for n in entries if n.count("/") >= 2 and n.split("/")[1].lower().endswith(".vpz")} | {n for n in entries if n.lower().endswith(".vpx")})
    check(not any(n.endswith(".DS_Store") for n in entries) and "art/layer.psd" in exp.get("possibleSourceFiles", []), "system files skipped, authoring files flagged")
    dry = t.call("vpx_export", {"dryRun": True, "exclude": ["art/"]})
    check(not any("/art/" in n for n in dry["contents"]) and dry.get("excluded") == ["art/"], "exclude leaves a folder out")
    check(tables == ["Smoke.vpz"] and any(n.endswith("/Smoke.vpz/manifest.json") for n in entries) and not any("/cache/" in n for n in entries) and "Smoke old.vpz" in exp.get("otherTablesLeftOut", []),
          f"vpxz package holds one table pack in its folder, no cache, other versions left out ({len(entries)} files)")
    shutil.rmtree(os.path.join(t.workdir, "Smoke old.vpz"))


def step_geometry_exports(t):
    shot_img = t.call("vpx_screenshot", {"maxSize": 400})
    check(isinstance(shot_img, str) and "screenshot" in shot_img.lower(), "screenshot captured")
    prim = next(n for n in t.part_names("Primitive") if n != "playfield_mesh")
    bounds = t.call("vpx_part", {"action": "get", "name": prim, "fields": ["position"]}).get("bounds")
    verts = t.call("vpx_geometry", {"action": "vertices", "name": prim, "max": 20})
    check(bounds and all(bounds["min"][i] <= v[i] + 0.11 and v[i] <= bounds["max"][i] + 0.11 for v in verts["vertices"] for i in range(3)), f"{prim} vertices lie inside its reported bounds")
    for kind in ("Ramp", "Bumper", "Wall", "Flipper"):
        names = t.part_names(kind)
        if not names:
            continue
        obj_path = os.path.join(t.workdir, f"smoke-{kind}.obj")
        m = t.call("vpx_geometry", {"action": "vertices", "name": names[0], "path": obj_path})
        with open(obj_path) as f:
            faces = sum(1 for line in f.read().splitlines() if line.startswith("f "))
        check(m["vertexCount"] > 0 and m["triangles"] > 0 and faces == m["triangles"], f"{kind} {names[0]} mesh exported ({m['vertexCount']} vertices, {m['triangles']} triangles)")
        os.remove(obj_path)
    rc = t.call("vpx_geometry", {"action": "rampcheck"})
    check("joins" in rc and "wiresInBallPath" in rc and "warnings" in rc and len(rc["ramps"]) > 0, f"rampcheck ran on {len(rc['ramps'])} ramps ({len(rc['warnings'])} warnings)")


def step_ramps_and_wireforms(t):
    fit = t.call("vpx_ramp", {"action": "fit", "rampcheck": False})
    check("ramps" in fit and all("rules" in r or "skipped" in r or "error" in r for r in fit["ramps"]), f"ramp fit graded {len(fit['ramps'])} ramps")
    plan = t.call("vpx_ramp", {"action": "plan", "points": RAMP_PLAN_POINTS})
    check(len(plan["points"]) > 10 and len(plan["bends"]) == 4 and all(abs(b["radius"] - 150) < 1 for b in plan["bends"]) and plan["heightTop"] == 60 and 30 < plan["climbSpeedNeeded"] < 40,
          f"ramp plan: {len(plan['points'])} drag points, {len(plan['bends'])} bends at 150, climb speed {plan['climbSpeedNeeded']}")
    spread = t.call("vpx_ramp", {"action": "plan", "exit": "spread", "points": RAMP_PLAN_POINTS})
    check(50 < spread["exitExtension"] < 70 and spread["length"] > plan["length"] + 50, f"ramp plan exit spread runs the ramp on by {spread['exitExtension']}")
    applied = t.call("vpx_ramp", {"action": "apply", "name": "SmokePlanRamp", "points": RAMP_PLAN_POINTS}, timeout=300)
    check(applied.get("created") and applied["fit"]["collisionWidth"] == 60 and applied["fit"]["top"]["inside"] and applied["fit"]["effectiveSlopeDeg"] > 4, f"ramp apply created SmokePlanRamp ({applied['fit']['verdict']}: {len(applied['fit'].get('findings', []))} findings)")
    wire = t.call("vpx_wireform", {"name": "SmokeWire", "ramp": "SmokePlanRamp"}, timeout=120)
    kinds = {}
    for fx in wire.get("fixtures", []):
        kinds[fx["type"]] = kinds.get(fx["type"], 0) + 1
    check(wire["rails"] == 4 and kinds.get("stirrup", 0) >= 5 and kinds.get("leg", 0) >= 4 and kinds.get("spread", 0) == 1 and kinds.get("tab", 0) == 1 and kinds.get("pullBack", 0) == 1 and wire["triangles"] > 1000,
          f"wireform built on the plan ramp: {wire['triangles']} triangles, fixtures {kinds}")
    sheet = t.call("vpx_ramp", {"action": "sheet", "ramp": "SmokePlanRamp", "name": "SmokeSheet", "finish": "metal", "handover": 30}, timeout=120)
    check(sheet.get("created") and sheet["floorWidth"] == 69 and sheet["flange"] == 6 and len(sheet["walls"]) == 2 and sheet.get("handover") == 30 and sheet["material"] == "Ramp Metal" and sheet["triangles"] > 100,
          f"sheet ramp built on the plan ramp: {sheet['triangles']} triangles, material {sheet['material']}")
    t.call("vpx_part", {"action": "delete", "names": ["SmokeWire", "SmokeSheet", "SmokePlanRamp"]})


def step_library_and_previews(t):
    lib = t.call("vpx_library", {"action": "list"})
    check(any(d["path"].endswith("exampleTable.vpz") and d.get("bundled") for d in lib["donors"]), "library lists the bundled example table as a donor")
    for mode in ("desktop", "vr"):
        img = t.call("vpx_screenshot", {"mode": mode, "maxSize": 300})
        check(isinstance(img, str) and "mode preview" in img, f"{mode} mode preview captured")
    check(t.status()["editorOpened"], "editor view restored after the previews")


STEPS = [
    step_protocol,
    step_bare_parts,
    step_geometry_rebuilds,
    step_stored_definitions,
    step_schemas,
    step_script_and_audit,
    step_drawing_order,
    step_table_properties,
    step_geometry_queries,
    step_generated_meshes,
    step_single_part_transform,
    step_images,
    step_sounds,
    step_collections,
    step_materials,
    step_donor_library,
    step_clone_bumpers,
    step_playtest_start,
    step_sim_shot_and_plunge,
    step_sim_flips,
    step_script_errors,
    step_sim_sweeps_and_tracking,
    step_background_jobs,
    step_scenarios,
    step_captures_in_play,
    step_stuckscan_and_autoplay,
    step_playtest_stop,
    step_editor_views,
    step_group_transform_undo_redo,
    step_selection_edits,
    step_undo_clones,
    step_reopen_table,
    step_export_package,
    step_geometry_exports,
    step_ramps_and_wireforms,
    step_library_and_previews,
]


def step_name(step):
    return step.__name__[len("step_"):]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", nargs="?")
    parser.add_argument("--port", type=int, default=2114)
    parser.add_argument("--token", default="")
    parser.add_argument("--keep", action="store_true", help="leave VPX running at the end")
    parser.add_argument("--list", action="store_true", help="list the steps and exit")
    parser.add_argument("--until", help="stop after this step (the steps depend on the ones before them)")
    args = parser.parse_args()
    if args.list:
        print("\n".join(step_name(s) for s in STEPS))
        return 0
    if not args.executable:
        parser.error("the VPinballX executable is required")
    steps = STEPS
    if args.until:
        if args.until not in [step_name(s) for s in STEPS]:
            parser.error(f"unknown step {args.until}, see --list")
        steps = STEPS[: [step_name(s) for s in STEPS].index(args.until) + 1]

    executable = resolve_executable(args.executable)
    workdir = tempfile.mkdtemp(prefix="vpx-mcp-smoke-")
    table = os.path.join(workdir, "Smoke.vpz")
    shutil.copytree(find_asset(executable, "blankTable.vpz"), table)
    log = open(os.path.join(workdir, "vpx.log"), "w")
    proc = subprocess.Popen([executable, "-LiveEdit", table], stdout=log, stderr=subprocess.STDOUT)
    port = wait_listening_port(os.path.join(workdir, "vpx.log"), args.port, 60)
    if port != args.port:
        print(f"[note] the smoke instance listens on port {port} (port {args.port} is taken by another VPX)")
    smoke = Smoke(Client(port, args.token), port, executable, workdir, table)
    failed = False
    started = time.time()
    try:
        for step in steps:
            print(f"--- {step_name(step)}")
            step(smoke)
        print(f"ALL {CHECKS} CHECKS PASSED in {time.time() - started:.0f} s")
    except (AssertionError, RuntimeError, urllib.error.URLError, OSError) as e:
        failed = True
        print(f"FAILED in {step_name(step)}: {e}")
        print(f"log: {log.name}")
    finally:
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
            if not failed:
                shutil.rmtree(workdir, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
