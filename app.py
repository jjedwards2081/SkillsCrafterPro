"""
Skills Crafter - Minecraft Education WebSocket Dashboard.

A web application that connects to Minecraft Education Edition clients,
displays player activity logs, and shows a real-time 2D map of player positions.
"""

import io
import json
import os
import socket
import time
import uuid
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO
from minecraft_server import MinecraftWSServer
from settings_manager import (
    load_settings, save_settings, mask_api_key,
    load_rubrics, save_rubrics,
)

app = Flask(__name__)
app.config["SECRET_KEY"] = "skills-crafter-secret"
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

mc_server = MinecraftWSServer(socketio=socketio)


def get_public_host():
    """Get the public hostname/IP for display in the connect string.
    Uses EXTERNAL_HOST env var when deployed, falls back to local IP."""
    ext = os.environ.get("EXTERNAL_HOST")
    if ext:
        return ext
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _get_llm_client():
    """Get an LLM client from the saved settings. Returns (client, provider, model)."""
    settings = load_settings()
    provider = settings.get("llm_provider", "")
    api_key = settings.get("llm_api_key", "")
    endpoint = settings.get("llm_endpoint", "")

    if not provider or not api_key:
        return None, None, None

    if provider == "openai":
        import openai
        return openai.OpenAI(api_key=api_key), "openai", "gpt-4o-mini"
    elif provider == "anthropic":
        import anthropic
        return anthropic.Anthropic(api_key=api_key), "anthropic", "claude-haiku-4-5-20251001"
    elif provider == "azure":
        import openai
        return openai.AzureOpenAI(
            api_key=api_key, azure_endpoint=endpoint, api_version="2024-02-01"
        ), "azure", "gpt-4o-mini"
    return None, None, None


def _llm_chat(prompt, system=None):
    """Send a chat message to the configured LLM. Returns the response text."""
    client, provider, model = _get_llm_client()
    if not client:
        raise ValueError("No LLM configured")

    if provider == "anthropic":
        kwargs = {"model": model, "max_tokens": 4096,
                  "messages": [{"role": "user", "content": prompt}]}
        if system:
            kwargs["system"] = system
        resp = client.messages.create(**kwargs)
        return resp.content[0].text
    else:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(model=model, messages=messages)
        return resp.choices[0].message.content


# ─── Page Routes ───

@app.route("/")
def index():
    local_ip = get_public_host()
    settings = load_settings()
    return render_template(
        "index.html",
        local_ip=local_ip,
        ws_port=mc_server.port,
        server_running=mc_server.running,
        settings=settings,
        masked_key=mask_api_key(settings.get("llm_api_key", "")),
    )


# ─── Settings API ───

@app.route("/api/settings", methods=["GET"])
def get_settings():
    settings = load_settings()
    masked = dict(settings)
    masked["llm_api_key"] = mask_api_key(settings.get("llm_api_key", ""))
    return jsonify(masked)


@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.get_json()
    current = load_settings()

    if "llm_provider" in data:
        current["llm_provider"] = data["llm_provider"]
    if "llm_endpoint" in data:
        current["llm_endpoint"] = data["llm_endpoint"]
    if "welcome_message" in data:
        current["welcome_message"] = data["welcome_message"]
    if "welcome_color" in data:
        current["welcome_color"] = data["welcome_color"]
    if "show_trace_paths" in data:
        current["show_trace_paths"] = data["show_trace_paths"]

    if "llm_api_key" in data and data["llm_api_key"] and "*" not in data["llm_api_key"]:
        current["llm_api_key"] = data["llm_api_key"]

    save_settings(current)
    mc_server.welcome_message = current["welcome_message"]
    mc_server.welcome_color = current["welcome_color"]

    masked = dict(current)
    masked["llm_api_key"] = mask_api_key(current.get("llm_api_key", ""))
    return jsonify({"status": "ok", "settings": masked})


@app.route("/api/settings/test-key", methods=["POST"])
def test_api_key():
    data = request.get_json()
    provider = data.get("provider", "")
    api_key = data.get("api_key", "")
    endpoint = data.get("endpoint", "")

    if not api_key:
        settings = load_settings()
        provider = provider or settings.get("llm_provider", "")
        api_key = settings.get("llm_api_key", "")
        endpoint = endpoint or settings.get("llm_endpoint", "")

    if not provider or not api_key:
        return jsonify({"connected": False, "error": "No provider or API key configured"})

    import time as _time
    max_retries = 3
    last_err = ""
    for attempt in range(max_retries):
        try:
            if provider == "openai":
                import openai
                openai.OpenAI(api_key=api_key).models.list()
            elif provider == "anthropic":
                import anthropic
                anthropic.Anthropic(api_key=api_key).messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=1,
                    messages=[{"role": "user", "content": "hi"}])
            elif provider == "azure":
                import openai
                openai.AzureOpenAI(
                    api_key=api_key, azure_endpoint=endpoint, api_version="2024-02-01"
                ).models.list()
            else:
                return jsonify({"connected": False, "error": f"Unknown provider: {provider}"})
            return jsonify({"connected": True})
        except Exception as e:
            last_err = str(e)[:200]
            # Retry on transient errors (overloaded, rate limit, 5xx)
            if any(code in last_err for code in ("529", "overloaded", "rate", "500", "502", "503")):
                if attempt < max_retries - 1:
                    _time.sleep(2 * (attempt + 1))
                    continue
            # Auth errors should not retry
            break
    return jsonify({"connected": False, "error": last_err})


@app.route("/api/settings/delete-key", methods=["POST"])
def delete_api_key():
    current = load_settings()
    current["llm_api_key"] = ""
    current["llm_provider"] = ""
    current["llm_endpoint"] = ""
    save_settings(current)
    return jsonify({"status": "ok"})


# ─── Rubric API ───

@app.route("/api/rubrics", methods=["GET"])
def get_rubrics():
    return jsonify(load_rubrics())


@app.route("/api/rubrics", methods=["POST"])
def add_rubric():
    data = request.get_json()
    rubrics = load_rubrics()
    rubric = {
        "id": str(uuid.uuid4())[:8],
        "name": data.get("name", "Untitled Rubric"),
        "criteria": data.get("criteria", []),
        "created": time.strftime("%Y-%m-%d %H:%M"),
    }
    rubrics.append(rubric)
    save_rubrics(rubrics)
    return jsonify({"status": "ok", "rubric": rubric})


@app.route("/api/rubrics/<rubric_id>", methods=["DELETE"])
def delete_rubric(rubric_id):
    rubrics = [r for r in load_rubrics() if r["id"] != rubric_id]
    save_rubrics(rubrics)
    return jsonify({"status": "ok"})


@app.route("/api/rubrics/<rubric_id>", methods=["PUT"])
def update_rubric(rubric_id):
    """Update an existing rubric."""
    data = request.get_json()
    rubrics = load_rubrics()
    for r in rubrics:
        if r["id"] == rubric_id:
            if "name" in data:
                r["name"] = data["name"]
            if "criteria" in data:
                r["criteria"] = data["criteria"]
            break
    save_rubrics(rubrics)
    return jsonify({"status": "ok"})


@app.route("/api/rubrics/suggest", methods=["POST"])
def suggest_criteria():
    """Use the LLM to suggest rubric criteria from a broad description."""
    data = request.get_json()
    description = data.get("description", "").strip()
    if not description:
        return jsonify({"error": "Please describe what you would like to assess."}), 400

    try:
        system = (
            "You are an education assessment expert specialising in Minecraft Education activities. "
            "Based on the user's description of what they want to assess, suggest appropriate rubric criteria. "
            "Return ONLY valid JSON with this structure: "
            '{"name": "Suggested Rubric Name", "criteria": [{"name": "Criterion Name", "description": "What to assess and what evidence to look for in Minecraft activity data"}]}'
            " Suggest 3-6 focused, observable criteria relevant to Minecraft Education gameplay."
        )
        result = _llm_chat(description, system=system)
        start = result.find("{")
        end = result.rfind("}") + 1
        if start >= 0 and end > start:
            suggestion = json.loads(result[start:end])
        else:
            raise ValueError("LLM did not return valid JSON")
        return jsonify({"status": "ok", "suggestion": suggestion})
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/api/rubrics/generate", methods=["POST"])
def generate_rubric():
    """Use the LLM to generate a rubric from uploaded file content."""
    file_text = ""
    if "file" in request.files:
        f = request.files["file"]
        filename = (f.filename or "").lower()
        raw_bytes = f.read()

        if filename.endswith(".pdf"):
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(stream=raw_bytes, filetype="pdf")
                pages = []
                for page in doc:
                    pages.append(page.get_text())
                doc.close()
                file_text = "\n\n".join(pages)
            except Exception as e:
                return jsonify({"error": f"Failed to read PDF: {e}"}), 400
        else:
            file_text = raw_bytes.decode("utf-8", errors="replace")

    elif request.is_json:
        file_text = request.get_json().get("text", "")

    if not file_text or not file_text.strip():
        return jsonify({"error": "No readable content found in the file"}), 400

    try:
        system = (
            "You are an education assessment expert. Generate a rubric from the provided content. "
            "Return ONLY valid JSON with this structure: "
            '{"name": "Rubric Name", "criteria": [{"name": "Criterion", "description": "What to assess", '
            '"levels": [{"label": "Excellent", "points": 4, "description": "..."}, '
            '{"label": "Good", "points": 3, "description": "..."}, '
            '{"label": "Developing", "points": 2, "description": "..."}, '
            '{"label": "Beginning", "points": 1, "description": "..."}]}]}'
        )
        result = _llm_chat(file_text[:8000], system=system)

        # Extract JSON from response
        start = result.find("{")
        end = result.rfind("}") + 1
        if start >= 0 and end > start:
            rubric_data = json.loads(result[start:end])
        else:
            raise ValueError("LLM did not return valid JSON")

        return jsonify({"status": "ok", "rubric": rubric_data})
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


# ─── Assessment API ───

@app.route("/api/player-stats", methods=["GET"])
def get_player_stats():
    """Return current player stats for the frontend."""
    stats = mc_server.get_player_stats()
    return jsonify(stats)


def _make_movement_graph(player_name, trail):
    """Generate a 2D movement graph (XZ plane) and return as bytes."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(1, 1, figsize=(5, 4))
    if not trail:
        ax.text(0.5, 0.5, "No movement data", ha="center", va="center",
                transform=ax.transAxes, color="#a19f9d")
    else:
        xs = [p[1] for p in trail]
        zs = [p[3] for p in trail]
        ax.plot(xs, zs, linewidth=1.2, color="#0078d4", alpha=0.7)
        ax.plot(xs[0], zs[0], "o", color="#107c10", markersize=8, label="Start")
        ax.plot(xs[-1], zs[-1], "s", color="#d13438", markersize=8, label="End")
        ax.legend(fontsize=8)

    ax.set_xlabel("X")
    ax.set_ylabel("Z")
    ax.set_title(f"{player_name} - Movement Path (Top-Down)", fontsize=11)
    ax.set_aspect("equal", adjustable="datalim")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf


def _format_duration(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


@app.route("/api/assess", methods=["POST"])
def run_assessment():
    """Assess selected players against a rubric using the LLM, return Word doc."""
    data = request.get_json()
    rubric_id = data.get("rubric_id")
    player_names = data.get("players", [])
    player_logs = data.get("player_logs", [])

    rubrics = load_rubrics()
    rubric = next((r for r in rubrics if r["id"] == rubric_id), None)
    if not rubric:
        return jsonify({"error": "Rubric not found"}), 404

    # Gather real player stats from the server
    all_stats = mc_server.get_player_stats()
    selected_stats = {n: all_stats[n] for n in player_names if n in all_stats}

    if not selected_stats:
        return jsonify({"error": "No player data available. Players must connect and generate activity before an assessment can be run."}), 400

    # Determine LLM model name for the report
    settings = load_settings()
    provider = settings.get("llm_provider", "unknown")
    _, _, model_name = _get_llm_client()
    model_label = f"{provider.upper()} / {model_name}" if model_name else "Unknown"
    assessment_time = time.strftime("%Y-%m-%d %H:%M:%S")

    # Build a data summary per player for the LLM
    player_summaries = {}
    for name, s in selected_stats.items():
        duration = s.get("time_connected_seconds", 0)
        player_summaries[name] = {
            "time_connected": _format_duration(duration),
            "blocks_placed": s.get("blocks_placed", 0),
            "blocks_placed_types": s.get("blocks_placed_types", {}),
            "blocks_broken": s.get("blocks_broken", 0),
            "blocks_broken_types": s.get("blocks_broken_types", {}),
            "items_acquired": s.get("items_acquired", 0),
            "mobs_killed": s.get("mobs_killed", 0),
            "messages_sent": s.get("messages_sent", 0),
            "messages": s.get("messages", [])[-20:],
            "distance_travelled": round(s.get("distance_travelled", 0), 1),
            "events_total": s.get("events_total", 0),
            "items_used": s.get("items_used", 0),
            "position_trail_length": len(s.get("position_trail", [])),
        }

    criteria_text = "\n".join(
        f"- {c['name']}: {c['description']}" for c in rubric.get("criteria", [])
    )

    prompt = (
        f"You are assessing Minecraft Education players based on observed in-game activity data.\n\n"
        f"RUBRIC: {rubric['name']}\n"
        f"CRITERIA:\n{criteria_text}\n\n"
        f"PLAYER DATA:\n{json.dumps(player_summaries, indent=2)}\n\n"
        f"RECENT EVENT LOG (last entries):\n{json.dumps(player_logs[-150:], indent=1)}\n\n"
        f"INSTRUCTIONS:\n"
        f"- Do NOT assign grades or scores. This is a qualitative, descriptive assessment.\n"
        f"- For EACH player, assess EACH criterion INDEPENDENTLY with observations and evidence from the data.\n"
        f"- If there is INSUFFICIENT data to reasonably assess a criterion, explicitly state this "
        f"and explain what data would be needed.\n"
        f"- After all individual criteria, provide a SYNOPTIC ASSESSMENT that considers the player's "
        f"activity holistically across all criteria.\n"
        f"- Be specific - reference actual numbers, actions, and behaviours from the data.\n\n"
        f"Return ONLY valid JSON with this structure:\n"
        f'{{"assessments": [{{"player": "name", '
        f'"criteria_assessments": [{{"criterion": "name", "observation": "detailed assessment or insufficient data note", '
        f'"sufficient_data": true/false}}], '
        f'"synoptic_assessment": "holistic assessment across all criteria"}}]}}'
    )

    try:
        system = "You are an education assessment expert. Return ONLY valid JSON. Do not assign grades or scores."
        result = _llm_chat(prompt, system=system)
        start = result.find("{")
        end = result.rfind("}") + 1
        assessment = json.loads(result[start:end])
    except Exception as e:
        return jsonify({"error": f"LLM assessment failed: {str(e)[:200]}"}), 500

    # ── Generate Word document ──
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    # Title page info
    title = doc.add_heading("Skills Crafter Assessment Report", level=0)
    doc.add_paragraph("")

    # Metadata table
    meta_table = doc.add_table(rows=5, cols=2)
    meta_table.style = "Light List Accent 1"
    meta_data = [
        ("Date & Time", assessment_time),
        ("AI Model", model_label),
        ("Rubric", rubric["name"]),
        ("Players Assessed", ", ".join(player_names)),
        ("Criteria Count", str(len(rubric.get("criteria", [])))),
    ]
    for i, (label, val) in enumerate(meta_data):
        meta_table.rows[i].cells[0].text = label
        meta_table.rows[i].cells[1].text = val
        for cell in meta_table.rows[i].cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(10)

    doc.add_paragraph("")

    # Rubric description
    doc.add_heading("Rubric Criteria", level=1)
    for c in rubric.get("criteria", []):
        p = doc.add_paragraph()
        run = p.add_run(f"{c['name']}: ")
        run.bold = True
        p.add_run(c.get("description", ""))
    doc.add_paragraph("")

    # Per-player sections
    for entry in assessment.get("assessments", []):
        pname = entry.get("player", "Unknown")
        doc.add_heading(f"Player: {pname}", level=1)

        ps = player_summaries.get(pname, {})
        ss = selected_stats.get(pname, {})

        # Activity statistics table
        doc.add_heading("Activity Summary", level=2)
        stat_table = doc.add_table(rows=1, cols=2)
        stat_table.style = "Light List Accent 1"
        stat_table.rows[0].cells[0].text = "Metric"
        stat_table.rows[0].cells[1].text = "Value"
        stat_rows = [
            ("Time Connected", ps.get("time_connected", "N/A")),
            ("Distance Travelled", f"{ps.get('distance_travelled', 0)} blocks"),
            ("Blocks Placed", str(ps.get("blocks_placed", 0))),
            ("Blocks Broken", str(ps.get("blocks_broken", 0))),
            ("Items Acquired", str(ps.get("items_acquired", 0))),
            ("Items Used", str(ps.get("items_used", 0))),
            ("Mobs Killed", str(ps.get("mobs_killed", 0))),
            ("Chat Messages", str(ps.get("messages_sent", 0))),
            ("Total Events", str(ps.get("events_total", 0))),
        ]
        for label, val in stat_rows:
            row = stat_table.add_row()
            row.cells[0].text = label
            row.cells[1].text = val

        # Block type breakdown if any
        bp_types = ps.get("blocks_placed_types", {})
        if bp_types:
            doc.add_paragraph("")
            p = doc.add_paragraph()
            run = p.add_run("Blocks Placed Breakdown: ")
            run.bold = True
            p.add_run(", ".join(f"{b} ({c})" for b, c in sorted(bp_types.items(), key=lambda x: -x[1])[:10]))

        bb_types = ss.get("blocks_broken_types", {})
        if bb_types:
            p = doc.add_paragraph()
            run = p.add_run("Blocks Broken Breakdown: ")
            run.bold = True
            p.add_run(", ".join(f"{b} ({c})" for b, c in sorted(bb_types.items(), key=lambda x: -x[1])[:10]))

        doc.add_paragraph("")

        # Movement graph
        doc.add_heading("Movement Path", level=2)
        trail = ss.get("position_trail", [])
        graph_buf = _make_movement_graph(pname, trail)
        doc.add_picture(graph_buf, width=Inches(4.5))
        doc.add_paragraph("")

        # Per-criterion assessments
        doc.add_heading("Criterion Assessments", level=2)
        for ca in entry.get("criteria_assessments", []):
            p = doc.add_paragraph()
            run = p.add_run(ca.get("criterion", "") + ": ")
            run.bold = True
            run.font.size = Pt(11)

            if not ca.get("sufficient_data", True):
                warn = p.add_run("[INSUFFICIENT DATA] ")
                warn.bold = True
                warn.font.color.rgb = RGBColor(0xD1, 0x34, 0x38)

            p.add_run(ca.get("observation", ""))

        doc.add_paragraph("")

        # Synoptic assessment
        doc.add_heading("Synoptic Assessment", level=2)
        doc.add_paragraph(entry.get("synoptic_assessment", "No synoptic assessment available."))
        doc.add_page_break()

    # Footer note
    p = doc.add_paragraph()
    run = p.add_run(f"Report generated by Skills Crafter on {assessment_time} using {model_label}. "
                     "This is an AI-assisted qualitative assessment based on observed in-game activity data. "
                     "No grades have been assigned.")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x60, 0x5E, 0x5C)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    filename = f"assessment-{time.strftime('%Y%m%d-%H%M%S')}.docx"
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


# ─── Game Commands API ───

@socketio.on("game_command")
def handle_game_command(data):
    """Send a Minecraft command to connected players."""
    command = data.get("command", "")
    targets = data.get("targets", "@a")  # default all players

    if not mc_server.running:
        socketio.emit("command_result", {"success": False, "error": "Server not running"})
        return

    # Replace @s with specific targets if needed
    final_cmd = command.replace("@s", targets)
    mc_server.send_command(final_cmd)
    socketio.emit("command_result", {"success": True, "command": final_cmd})


# ─── WebSocket Events ───

@socketio.on("start_server")
def handle_start_server():
    if not mc_server.running:
        settings = load_settings()
        mc_server.welcome_message = settings["welcome_message"]
        mc_server.welcome_color = settings["welcome_color"]
        mc_server.start()


@socketio.on("stop_server")
def handle_stop_server():
    if mc_server.running:
        mc_server.stop()


@socketio.on("connect")
def handle_web_connect():
    socketio.emit("server_status", {
        "running": mc_server.running,
        "host": mc_server.host,
        "port": mc_server.port,
    })
    socketio.emit("players_update", mc_server._get_players_data())


if __name__ == "__main__":
    settings = load_settings()
    mc_server.welcome_message = settings["welcome_message"]
    mc_server.welcome_color = settings["welcome_color"]

    print("=" * 50)
    print("  Skills Crafter - Minecraft Education Dashboard")
    print("=" * 50)
    print(f"  Open http://localhost:5050 in your browser")
    print(f"  Local IP: {get_public_host()}")
    print("=" * 50)
    socketio.run(app, host="0.0.0.0", port=5050, debug=False, allow_unsafe_werkzeug=True)
