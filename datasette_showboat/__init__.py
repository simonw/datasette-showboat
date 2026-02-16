from datasette import hookimpl, Response
import base64
import datetime
import email
import email.policy
import html as html_module
import urllib.parse


def get_db(datasette):
    config = datasette.plugin_config("datasette-showboat") or {}
    db_name = config.get("database")
    if db_name:
        return datasette.get_database(db_name)
    return datasette.get_internal_database()


def get_token(datasette):
    config = datasette.plugin_config("datasette-showboat") or {}
    return config.get("token")


def make_fence(content):
    """Return a backtick fence string that doesn't conflict with content."""
    max_run = 0
    current_run = 0
    for char in content:
        if char == "`":
            current_run += 1
            max_run = max(max_run, current_run)
        else:
            current_run = 0
    return "`" * max(3, max_run + 1)


def parse_multipart_body(content_type, body):
    """Parse multipart/form-data into (fields_dict, files_dict) using stdlib email."""
    header = b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("latin-1") + b"\r\n\r\n"
    raw = header + body
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    fields = {}
    files = {}
    for part in msg.iter_parts():
        cd = part.get("Content-Disposition", "")
        if "form-data" not in cd:
            continue
        name = part.get_param("name", header="content-disposition")
        filename = part.get_param("filename", header="content-disposition")
        if filename:
            files[name] = part.get_payload(decode=True)
        else:
            payload = part.get_payload(decode=True)
            fields[name] = payload.decode("utf-8") if payload else ""
    return fields, files


@hookimpl
def startup(datasette):
    async def inner():
        db = get_db(datasette)
        await db.execute_write(
            """
            CREATE TABLE IF NOT EXISTS showboat_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                showboat_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                markdown TEXT NOT NULL,
                image BLOB
            )
            """
        )
        await db.execute_write(
            """
            CREATE INDEX IF NOT EXISTS idx_showboat_chunks_showboat_id
            ON showboat_chunks (showboat_id)
            """
        )

    return inner


@hookimpl
def skip_csrf(scope):
    return scope.get("type") == "http" and scope.get("path") == "/-/showboat/receive"


@hookimpl
def register_routes():
    return [
        (r"^/-/showboat/receive$", showboat_receive),
        (r"^/-/showboat/(?P<uuid>[^/]+)\.json$", showboat_document_json),
        (r"^/-/showboat/(?P<uuid>[^/]+)$", showboat_document),
        (r"^/-/showboat$", showboat_index),
    ]


# --- Route handlers ---


async def showboat_receive(request, datasette):
    if request.method != "POST":
        return Response.json({"error": "Method not allowed"}, status=405)

    # Token authentication
    expected_token = get_token(datasette)
    if expected_token:
        provided_token = request.args.get("token")
        if provided_token != expected_token:
            return Response.json({"error": "Invalid token"}, status=403)

    # Parse body based on content type
    body = await request.post_body()
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        fields, files = parse_multipart_body(content_type, body)
    else:
        parsed = urllib.parse.parse_qs(body.decode("utf-8"))
        fields = {k: v[0] for k, v in parsed.items()}
        files = {}

    uuid = fields.get("uuid", "")
    command = fields.get("command", "")

    if not uuid or not command:
        return Response.json({"error": "uuid and command are required"}, status=400)

    db = get_db(datasette)
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if command == "init":
        title = fields.get("title", "Untitled")
        await db.execute_write(
            "INSERT INTO showboat_chunks (showboat_id, created_at, markdown, image) VALUES (?, ?, ?, ?)",
            [uuid, created_at, f"# {title}", None],
        )

    elif command == "note":
        markdown = fields.get("markdown", "")
        await db.execute_write(
            "INSERT INTO showboat_chunks (showboat_id, created_at, markdown, image) VALUES (?, ?, ?, ?)",
            [uuid, created_at, markdown, None],
        )

    elif command == "exec":
        language = fields.get("language", "")
        input_code = fields.get("input", "")
        output_text = fields.get("output", "")
        code_fence = make_fence(input_code)
        output_fence = make_fence(output_text)
        markdown = f"{code_fence}{language}\n{input_code}\n{code_fence}\n\n{output_fence}output\n{output_text}\n{output_fence}"
        await db.execute_write(
            "INSERT INTO showboat_chunks (showboat_id, created_at, markdown, image) VALUES (?, ?, ?, ?)",
            [uuid, created_at, markdown, None],
        )

    elif command == "image":
        input_text = fields.get("input", "")
        alt_text = fields.get("alt", "")
        image_data = files.get("image")
        fence = make_fence(input_text)
        markdown = f"{fence}bash {{image}}\n{input_text}\n{fence}"
        if alt_text:
            markdown += f"\n\n![{alt_text}]()"
        await db.execute_write(
            "INSERT INTO showboat_chunks (showboat_id, created_at, markdown, image) VALUES (?, ?, ?, ?)",
            [uuid, created_at, markdown, image_data],
        )

    elif command == "pop":
        await db.execute_write(
            """
            DELETE FROM showboat_chunks WHERE id = (
                SELECT id FROM showboat_chunks
                WHERE showboat_id = ?
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            [uuid],
        )
        return Response.json({"ok": True}, status=200)

    else:
        return Response.json({"error": f"Unknown command: {command}"}, status=400)

    return Response.json({"ok": True}, status=201)


async def showboat_document_json(request, datasette):
    uuid = request.url_vars["uuid"]
    db = get_db(datasette)
    after = request.args.get("after")

    if after:
        result = await db.execute(
            "SELECT id, showboat_id, created_at, markdown, image FROM showboat_chunks WHERE showboat_id = ? AND id > ? ORDER BY id",
            [uuid, int(after)],
        )
    else:
        result = await db.execute(
            "SELECT id, showboat_id, created_at, markdown, image FROM showboat_chunks WHERE showboat_id = ? ORDER BY id",
            [uuid],
        )

    chunks = []
    for row in result.rows:
        chunk = {
            "id": row[0],
            "showboat_id": row[1],
            "created_at": row[2],
            "markdown": row[3],
        }
        if row[4]:
            chunk["image"] = base64.b64encode(row[4]).decode("ascii")
        chunks.append(chunk)

    return Response.json({"chunks": chunks})


DOCUMENT_VIEWER_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Showboat Document</title>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            line-height: 1.6;
            color: #1f2328;
        }
        .chunk { margin-bottom: 0.5em; }
        .chunk-timestamp {
            font-size: 0.7em;
            color: #999;
            margin-bottom: 0.5em;
        }
        .chunk-content img { max-width: 100%; }
        pre {
            background: #f6f8fa;
            padding: 16px;
            border-radius: 6px;
            overflow-x: auto;
        }
        code {
            background: #f6f8fa;
            padding: 0.2em 0.4em;
            border-radius: 3px;
            font-size: 85%;
        }
        pre code { background: none; padding: 0; }
        a { color: #0969da; }
        .nav { margin-bottom: 1em; font-size: 0.9em; }
    </style>
</head>
<body>
    <div class="nav"><a href="/-/showboat">&larr; All documents</a></div>
    <div id="chunks"></div>
    <script>
    (function() {
        const uuid = "__UUID__";
        let lastId = 0;
        const chunksDiv = document.getElementById("chunks");

        function renderChunk(chunk) {
            const div = document.createElement("div");
            div.className = "chunk";
            div.setAttribute("data-id", chunk.id);

            const content = document.createElement("div");
            content.className = "chunk-content";
            let md = chunk.markdown;
            if (chunk.image) {
                const dataUri = "data:image/png;base64," + chunk.image;
                if (md.match(/!\\[[^\\]]*\\]\\(\\)/)) {
                    md = md.replace(/!\\[([^\\]]*)\\]\\(\\)/, "![$1](" + dataUri + ")");
                } else {
                    md += "\\n\\n![image](" + dataUri + ")";
                }
            }
            content.innerHTML = marked.parse(md);

            const ts = document.createElement("div");
            ts.className = "chunk-timestamp";
            const date = new Date(chunk.created_at);
            ts.textContent = date.toLocaleString();

            div.appendChild(content);
            div.appendChild(ts);
            chunksDiv.appendChild(div);
        }

        async function poll() {
            try {
                const url = "/-/showboat/" + uuid + ".json" + (lastId ? "?after=" + lastId : "");
                const response = await fetch(url);
                if (!response.ok) return;
                const data = await response.json();
                if (data.chunks && data.chunks.length > 0) {
                    data.chunks.forEach(function(chunk) {
                        renderChunk(chunk);
                        if (chunk.id > lastId) lastId = chunk.id;
                    });
                }
            } catch (e) {
                console.error("Poll error:", e);
            }
        }

        poll();
        setInterval(poll, 2000);
    })();
    </script>
</body>
</html>"""


async def showboat_document(request, datasette):
    uuid = request.url_vars["uuid"]
    return Response.html(DOCUMENT_VIEWER_HTML.replace("__UUID__", uuid))


INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Showboat</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            line-height: 1.6;
            color: #1f2328;
        }
        pre {
            background: #f6f8fa;
            padding: 16px;
            border-radius: 6px;
            overflow-x: auto;
        }
        code {
            background: #f6f8fa;
            padding: 0.2em 0.4em;
            border-radius: 3px;
        }
        pre code { background: none; padding: 0; }
        a { color: #0969da; }
        ul { padding-left: 1.5em; }
        .doc-meta { color: #666; font-size: 0.9em; }
    </style>
</head>
<body>
    <h1>Showboat Documents</h1>
    <ul>
    __DOCUMENTS__
    </ul>

    <h2>Setup</h2>
    <p>To send showboat output to this server, set the <code>SHOWBOAT_REMOTE_URL</code>
    environment variable to point to the receive endpoint:</p>
    <pre><code>export SHOWBOAT_REMOTE_URL="https://your-datasette-instance/-/showboat/receive"</code></pre>
    <p>If a token is configured, include it as a query parameter:</p>
    <pre><code>export SHOWBOAT_REMOTE_URL="https://your-datasette-instance/-/showboat/receive?token=your-token"</code></pre>
</body>
</html>"""


async def showboat_index(request, datasette):
    db = get_db(datasette)
    result = await db.execute(
        """
        SELECT
            showboat_id,
            COUNT(*) as chunk_count,
            MIN(created_at) as first_chunk,
            MAX(created_at) as last_chunk
        FROM showboat_chunks
        GROUP BY showboat_id
        ORDER BY MAX(created_at) DESC
        """
    )

    documents_html = ""
    for row in result.rows:
        showboat_id = html_module.escape(str(row[0]))
        chunk_count = row[1]
        first_chunk = html_module.escape(str(row[2]))
        last_chunk = html_module.escape(str(row[3]))
        documents_html += (
            f'<li><a href="/-/showboat/{showboat_id}">{showboat_id}</a>'
            f' <span class="doc-meta">- {chunk_count} chunks,'
            f" started {first_chunk}, last updated {last_chunk}</span></li>\n"
        )

    if not documents_html:
        documents_html = "<li>No documents yet.</li>"

    return Response.html(INDEX_HTML.replace("__DOCUMENTS__", documents_html))
