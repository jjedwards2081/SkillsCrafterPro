"""
Minecraft Bedrock Edition WebSocket Server.

Runs in a separate process to avoid eventlet monkey-patching conflicts.
Communicates with the Flask app via a multiprocessing Queue.
Tracks rich per-player activity statistics for assessment.
"""

import asyncio
import json
import uuid
import time
import multiprocessing
from websockets.asyncio.server import serve

SUBSCRIBED_EVENTS = [
    "PlayerMessage", "PlayerTransform", "PlayerTravelled",
    "PlayerJoin", "PlayerLeave", "BlockPlaced", "BlockBroken",
    "ItemAcquired", "ItemUsed", "MobKilled",
]


def _make_subscribe_message(event_name):
    return json.dumps({
        "header": {"version": 1, "requestId": str(uuid.uuid4()),
                   "messageType": "commandRequest", "messagePurpose": "subscribe"},
        "body": {"eventName": event_name},
    })


def _make_command_message(command):
    return json.dumps({
        "header": {"version": 1, "requestId": str(uuid.uuid4()),
                   "messageType": "commandRequest", "messagePurpose": "commandRequest"},
        "body": {"version": 1, "commandLine": command, "origin": {"type": "player"}},
    })


def _new_player_stats(name):
    """Create a fresh stats dict for a newly connected player."""
    return {
        "name": name,
        "connected_at": time.time(),
        "disconnected_at": None,
        "x": 0, "y": 0, "z": 0,
        "last_seen": time.time(),
        "position_trail": [],       # [(timestamp, x, y, z), ...]
        "blocks_placed": 0,
        "blocks_placed_types": {},   # block_name -> count
        "blocks_broken": 0,
        "blocks_broken_types": {},
        "items_acquired": 0,
        "items_acquired_types": {},
        "items_used": 0,
        "mobs_killed": 0,
        "mobs_killed_types": {},
        "messages_sent": 0,
        "messages": [],             # [{time, text}, ...]
        "distance_travelled": 0.0,
        "events_total": 0,
    }


def _ws_server_process(queue, cmd_queue, host, port, stop_event,
                        welcome_message, welcome_color):
    MC_COLOR_CODES = {
        'dark_red': '\u00a74', 'red': '\u00a7c', 'gold': '\u00a76',
        'yellow': '\u00a7e', 'dark_green': '\u00a72', 'green': '\u00a7a',
        'aqua': '\u00a7b', 'dark_aqua': '\u00a73', 'dark_blue': '\u00a71',
        'blue': '\u00a79', 'light_purple': '\u00a7d', 'dark_purple': '\u00a75',
        'white': '\u00a7f', 'gray': '\u00a77', 'dark_gray': '\u00a78',
        'black': '\u00a70',
    }

    player_stats = {}       # name -> stats dict
    connections = {}        # ws_id -> player_name
    active_websockets = {}  # ws_id -> websocket

    TRAIL_INTERVAL = 2.0    # seconds between trail samples
    last_trail_time = {}    # name -> last trail timestamp

    def send(event, data):
        try:
            queue.put_nowait((event, data))
        except Exception:
            pass

    def log(message, level="info"):
        send("log", {"timestamp": time.strftime("%H:%M:%S"), "message": message, "level": level})

    def get_players_data():
        """Return position data for the map UI."""
        return [{"name": s["name"], "x": s["x"], "y": s["y"], "z": s["z"],
                 "last_seen": s["last_seen"]} for s in player_stats.values()
                if s["disconnected_at"] is None]

    def get_all_stats():
        """Return full stats for assessment."""
        result = {}
        for name, s in player_stats.items():
            st = dict(s)
            # Compute connection duration
            end = s["disconnected_at"] or time.time()
            st["time_connected_seconds"] = round(end - s["connected_at"], 1)
            # Trim trail for serialisation (keep up to 500 points)
            st["position_trail"] = s["position_trail"][-500:]
            result[name] = st
        return result

    def ensure_stats(name):
        if name and name not in player_stats:
            player_stats[name] = _new_player_stats(name)
        return player_stats.get(name)

    def update_position(name, x, y, z):
        if not name:
            return
        s = ensure_stats(name)
        if not s:
            return

        # Calculate distance from last position
        dx = x - s["x"]
        dz = z - s["z"]
        dist = (dx*dx + dz*dz) ** 0.5
        s["distance_travelled"] += dist

        s["x"] = round(x, 1)
        s["y"] = round(y, 1)
        s["z"] = round(z, 1)
        s["last_seen"] = time.time()

        # Sample trail at intervals
        now = time.time()
        if now - last_trail_time.get(name, 0) >= TRAIL_INTERVAL:
            s["position_trail"].append((round(now, 1), s["x"], s["y"], s["z"]))
            last_trail_time[name] = now

        send("players_update", get_players_data())
        # Also send stats periodically (piggyback on position updates)
        send("player_stats_update", get_all_stats())

    def extract_player_info(body):
        properties = body.get("properties", {})
        player_obj = body.get("player", None)
        name = None
        pos = None
        if isinstance(player_obj, dict):
            name = player_obj.get("name", None)
            pos_obj = player_obj.get("position", None)
            if isinstance(pos_obj, dict):
                pos = (pos_obj.get("x", 0), pos_obj.get("y", 0), pos_obj.get("z", 0))
        if not name:
            name = (properties.get("Player", None) or properties.get("Sender", None)
                    or body.get("sender", None))
            if not isinstance(name, str):
                name = None
        if not pos and "PosX" in properties:
            try:
                pos = (properties["PosX"], properties["PosY"], properties["PosZ"])
            except (KeyError, TypeError):
                pass
        return name, pos

    def handle_event(ws_id, event_name, body):
        name, pos = extract_player_info(body)
        if not name or name == "Server":
            name = connections.get(ws_id)

        if name and name != "Server":
            current = connections.get(ws_id)
            if current != name:
                connections[ws_id] = name
                s = ensure_stats(name)
                log(f"Player identified: {name}", "success")
                send("player_joined", {"name": name})

        s = ensure_stats(name) if name else None
        if s:
            s["events_total"] += 1

        if event_name in ("PlayerTransform", "PlayerTravelled") and pos:
            update_position(name, *pos)
        elif event_name == "PlayerMessage":
            properties = body.get("properties", {})
            sender = properties.get("Sender", body.get("sender", name or "Unknown"))
            message = properties.get("Message", body.get("message", ""))
            if sender != "Server":
                log(f"[Chat] {sender}: {message}")
                if s:
                    s["messages_sent"] += 1
                    s["messages"].append({"time": time.strftime("%H:%M:%S"), "text": message})
        elif event_name == "PlayerJoin":
            log(f"Player joined the world: {name or 'Unknown'}", "success")
        elif event_name == "PlayerLeave":
            log(f"Player left the world: {name or 'Unknown'}", "warn")
            if s:
                s["disconnected_at"] = time.time()
        elif event_name == "BlockPlaced":
            properties = body.get("properties", {})
            block = properties.get("Block", body.get("block", {}).get("id", "unknown") if isinstance(body.get("block"), dict) else "unknown")
            log(f"{name} placed {block}")
            if s:
                s["blocks_placed"] += 1
                s["blocks_placed_types"][block] = s["blocks_placed_types"].get(block, 0) + 1
        elif event_name == "BlockBroken":
            properties = body.get("properties", {})
            block = properties.get("Block", "unknown")
            tool = properties.get("Tool", "hand")
            log(f"{name} broke {block} with {tool}")
            if s:
                s["blocks_broken"] += 1
                s["blocks_broken_types"][block] = s["blocks_broken_types"].get(block, 0) + 1
        elif event_name == "ItemAcquired":
            properties = body.get("properties", {})
            item = properties.get("Item", "unknown")
            count = properties.get("Count", 1)
            log(f"{name} acquired {count}x {item}")
            if s:
                s["items_acquired"] += int(count)
                s["items_acquired_types"][item] = s["items_acquired_types"].get(item, 0) + int(count)
        elif event_name == "ItemUsed":
            if s:
                s["items_used"] += 1
        elif event_name == "MobKilled":
            properties = body.get("properties", {})
            mob = properties.get("MobType", "unknown")
            log(f"{name} killed {mob}")
            if s:
                s["mobs_killed"] += 1
                s["mobs_killed_types"][mob] = s["mobs_killed_types"].get(mob, 0) + 1
        else:
            log(f"[{event_name}] {name or 'Unknown'}")

    async def handle_connection(websocket):
        ws_id = id(websocket)
        remote = websocket.remote_address
        log(f"New connection from {remote[0]}:{remote[1]}")
        connections[ws_id] = None
        active_websockets[ws_id] = websocket

        for event in SUBSCRIBED_EVENTS:
            await websocket.send(_make_subscribe_message(event))
        log(f"Subscribed to {len(SUBSCRIBED_EVENTS)} events")
        await websocket.send(_make_command_message("querytarget @s"))

        if welcome_message:
            color_code = MC_COLOR_CODES.get(welcome_color, '\u00a7a')
            raw = json.dumps({"rawtext": [{"text": color_code + welcome_message}]})
            await websocket.send(_make_command_message(f"tellraw @s {raw}"))
            log("Sent welcome message")

        try:
            async for raw_message in websocket:
                try:
                    msg = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                header = msg.get("header", {})
                body = msg.get("body", {})
                purpose = header.get("messagePurpose", "")
                event_name = header.get("eventName", "")
                if purpose == "commandResponse":
                    continue
                if purpose == "event":
                    handle_event(ws_id, event_name, body)
        except Exception as e:
            log(f"Connection error: {e}", "error")
        finally:
            active_websockets.pop(ws_id, None)
            player_name = connections.pop(ws_id, None)
            if player_name:
                s = player_stats.get(player_name)
                if s:
                    s["disconnected_at"] = time.time()
                log(f"Player disconnected: {player_name}", "warn")
                send("player_left", {"name": player_name})
                send("players_update", get_players_data())
                send("player_stats_update", get_all_stats())

    async def run_server():
        server = await serve(handle_connection, host, port)
        log(f"WebSocket server started on {host}:{port}", "success")
        send("server_status", {"running": True, "host": host, "port": port})

        while not stop_event.is_set():
            while not cmd_queue.empty():
                try:
                    command = cmd_queue.get_nowait()
                    msg = _make_command_message(command)
                    for ws in list(active_websockets.values()):
                        try:
                            await ws.send(msg)
                        except Exception:
                            pass
                    log(f"Sent command: {command}", "info")
                except Exception:
                    break
            await asyncio.sleep(0.3)

        server.close()
        await server.wait_closed()
        log("WebSocket server stopped", "warn")
        send("server_status", {"running": False})
        send("players_update", [])

    asyncio.run(run_server())


class MinecraftWSServer:
    """Manages the Minecraft WebSocket server as a separate process."""

    def __init__(self, socketio=None):
        self.socketio = socketio
        self.host = "0.0.0.0"
        self.port = 19131
        self.players = {}
        self.player_stats = {}  # full stats dict from subprocess
        self.welcome_message = ""
        self.welcome_color = "green"
        self._process = None
        self._queue = None
        self._cmd_queue = None
        self._stop_event = None
        self._reader_thread = None
        self.running = False

    def _queue_reader(self):
        while self.running:
            try:
                event, data = self._queue.get(timeout=0.5)
            except Exception:
                continue

            if event == "players_update":
                self.players = {p["name"]: p for p in data}
            elif event == "player_stats_update":
                self.player_stats = data
            elif event == "server_status" and not data.get("running"):
                self.running = False

            if self.socketio:
                self.socketio.emit(event, data)

    def _get_players_data(self):
        return list(self.players.values())

    def get_player_stats(self):
        """Return full player stats for assessment."""
        return dict(self.player_stats)

    def start(self):
        if self.running:
            return
        self._queue = multiprocessing.Queue()
        self._cmd_queue = multiprocessing.Queue()
        self._stop_event = multiprocessing.Event()
        self._process = multiprocessing.Process(
            target=_ws_server_process,
            args=(self._queue, self._cmd_queue, self.host, self.port, self._stop_event,
                  self.welcome_message, self.welcome_color),
            daemon=True,
        )
        self._process.start()
        self.running = True
        self.players.clear()
        self.player_stats.clear()

        import threading
        self._reader_thread = threading.Thread(target=self._queue_reader, daemon=True)
        self._reader_thread.start()

    def stop(self):
        if not self.running:
            return
        self._stop_event.set()
        if self._process:
            self._process.join(timeout=5)
            if self._process.is_alive():
                self._process.terminate()
            self._process = None
        self.running = False
        self.players.clear()
        if self.socketio:
            self.socketio.emit("server_status", {"running": False})
            self.socketio.emit("players_update", [])

    def send_command(self, command):
        if self.running and self._cmd_queue:
            try:
                self._cmd_queue.put_nowait(command)
            except Exception:
                pass
