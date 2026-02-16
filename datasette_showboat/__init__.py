from datasette import hookimpl, Response
from datasette.permissions import Action, PermissionSQL
import base64
import datetime


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


def render_markdown(chunk):
    """Compute markdown from a chunk's raw fields based on its command type."""
    command = chunk["command"]
    if command == "init":
        return f"# {chunk.get('title') or 'Untitled'}"
    elif command == "note":
        return chunk.get("markdown") or ""
    elif command == "exec":
        language = chunk.get("language") or ""
        input_code = chunk.get("input") or ""
        output_text = chunk.get("output") or ""
        code_fence = make_fence(input_code)
        output_fence = make_fence(output_text)
        return f"{code_fence}{language}\n{input_code}\n{code_fence}\n\n{output_fence}output\n{output_text}\n{output_fence}"
    elif command == "image":
        filename = chunk.get("filename") or ""
        alt_text = chunk.get("alt") or ""
        fence = make_fence(filename)
        md = f"{fence}bash {{image}}\n{filename}\n{fence}"
        if alt_text:
            md += f"\n\n![{alt_text}]()"
        return md
    return ""


@hookimpl
def startup(datasette):
    async def inner():
        db = get_db(datasette)
        await db.execute_write(
            """
            CREATE TABLE IF NOT EXISTS showboat_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                showboat_id TEXT NOT NULL,
                command TEXT NOT NULL,
                created_at TEXT NOT NULL,
                title TEXT,
                markdown TEXT,
                language TEXT,
                input TEXT,
                output TEXT,
                filename TEXT,
                alt TEXT,
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
def skip_csrf(datasette, scope):
    receive_path = datasette.urls.path("/-/showboat/receive")
    return scope.get("type") == "http" and scope.get("path") == receive_path


@hookimpl
def register_actions():
    return [
        Action(
            name="showboat",
            description="View showboat documents",
        ),
    ]


@hookimpl
def permission_resources_sql(datasette, actor, action):
    if action == "showboat":
        # Only provide default allow if showboat is not explicitly configured
        config_perms = (datasette.config or {}).get("permissions", {})
        metadata_perms = (datasette._metadata_local or {}).get("permissions", {})
        if "showboat" not in config_perms and "showboat" not in metadata_perms:
            return PermissionSQL.allow("Default allow for showboat")


@hookimpl
def menu_links(datasette, actor):
    async def inner():
        if await datasette.allowed(action="showboat", actor=actor):
            return [
                {
                    "href": datasette.urls.path("/-/showboat"),
                    "label": "Showboat",
                }
            ]
        return []

    return inner


@hookimpl
def register_routes():
    return [
        (r"^/-/showboat/receive$", showboat_receive),
        (r"^/-/showboat/(?P<uuid>[^/]+)\.json$", showboat_document_json),
        (r"^/-/showboat/(?P<uuid>[^/]+)$", showboat_document),
        (r"^/-/showboat$", showboat_index),
    ]


# --- Route handlers ---


COLUMNS = (
    "showboat_id", "command", "created_at", "title", "markdown",
    "language", "input", "output", "filename", "alt", "image",
)

INSERT_SQL = f"INSERT INTO showboat_chunks ({', '.join(COLUMNS)}) VALUES ({', '.join('?' for _ in COLUMNS)})"


async def showboat_receive(request, datasette):
    if request.method != "POST":
        return Response.json({"error": "Method not allowed"}, status=405)

    # Token authentication
    expected_token = get_token(datasette)
    if expected_token:
        provided_token = request.args.get("token")
        if provided_token != expected_token:
            return Response.json({"error": "Invalid token"}, status=403)

    # Parse form data (handles both url-encoded and multipart)
    form = await request.form(files=True)
    uuid = form.get("uuid", "")
    command = form.get("command", "")

    if not uuid or not command:
        return Response.json({"error": "uuid and command are required"}, status=400)

    db = get_db(datasette)
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if command == "init":
        title = form.get("title", "Untitled")
        await db.execute_write(INSERT_SQL, [
            uuid, "init", created_at, title, None,
            None, None, None, None, None, None,
        ])

    elif command == "note":
        markdown = form.get("markdown", "")
        await db.execute_write(INSERT_SQL, [
            uuid, "note", created_at, None, markdown,
            None, None, None, None, None, None,
        ])

    elif command == "exec":
        language = form.get("language", "")
        input_code = form.get("input", "")
        output_text = form.get("output", "")
        await db.execute_write(INSERT_SQL, [
            uuid, "exec", created_at, None, None,
            language, input_code, output_text, None, None, None,
        ])

    elif command == "image":
        filename = form.get("filename", "")
        alt_text = form.get("alt", "")
        uploaded = form.get("image")
        image_data = await uploaded.read() if uploaded and hasattr(uploaded, "read") else None
        await db.execute_write(INSERT_SQL, [
            uuid, "image", created_at, None, None,
            None, None, None, filename, alt_text, image_data,
        ])

    elif command == "pop":
        await db.execute_write(INSERT_SQL, [
            uuid, "pop", created_at, None, None,
            None, None, None, None, None, None,
        ])

    else:
        return Response.json({"error": f"Unknown command: {command}"}, status=400)

    return Response.json({"ok": True}, status=201)


SELECT_COLUMNS = "id, showboat_id, command, created_at, title, markdown, language, input, output, filename, alt, image"


async def showboat_document_json(request, datasette):
    await datasette.ensure_permission(action="showboat", actor=request.actor)
    uuid = request.url_vars["uuid"]
    db = get_db(datasette)
    after = request.args.get("after")

    if after:
        result = await db.execute(
            f"SELECT {SELECT_COLUMNS} FROM showboat_chunks WHERE showboat_id = ? AND id > ? ORDER BY id",
            [uuid, int(after)],
        )
    else:
        result = await db.execute(
            f"SELECT {SELECT_COLUMNS} FROM showboat_chunks WHERE showboat_id = ? ORDER BY id",
            [uuid],
        )

    chunks = []
    for row in result.rows:
        chunk = {
            "id": row[0],
            "showboat_id": row[1],
            "command": row[2],
            "created_at": row[3],
        }
        # Include non-null raw fields
        field_names = ["title", "markdown", "language", "input", "output", "filename", "alt"]
        for i, name in enumerate(field_names):
            val = row[4 + i]
            if val is not None:
                chunk[name] = val
        # Compute markdown for display
        if chunk["command"] != "pop":
            chunk["rendered_markdown"] = render_markdown(chunk)
        # Base64-encode image blob
        if row[11]:
            chunk["image"] = base64.b64encode(row[11]).decode("ascii")
        chunks.append(chunk)

    return Response.json({"chunks": chunks})


async def showboat_document(request, datasette):
    await datasette.ensure_permission(action="showboat", actor=request.actor)
    uuid = request.url_vars["uuid"]
    base_url = datasette.urls.path("/")
    json_url = datasette.urls.path(f"/-/showboat/{uuid}.json")
    return Response.html(
        await datasette.render_template(
            "showboat_document.html",
            {"uuid": uuid, "base_url": base_url, "json_url": json_url},
            request=request,
        )
    )


async def showboat_index(request, datasette):
    await datasette.ensure_permission(action="showboat", actor=request.actor)
    db = get_db(datasette)
    result = await db.execute(
        """
        SELECT
            showboat_id,
            COUNT(*) as chunk_count,
            MIN(created_at) as first_chunk,
            MAX(created_at) as last_chunk
        FROM showboat_chunks
        WHERE command != 'pop'
        GROUP BY showboat_id
        ORDER BY MAX(created_at) DESC
        """
    )

    documents = []
    for row in result.rows:
        documents.append(
            {
                "showboat_id": row[0],
                "chunk_count": row[1],
                "first_chunk": row[2],
                "last_chunk": row[3],
            }
        )

    base_url = datasette.urls.path("/")
    receive_path = datasette.urls.path("/-/showboat/receive")
    receive_url = f"{request.scheme}://{request.host}{receive_path}"
    return Response.html(
        await datasette.render_template(
            "showboat_index.html",
            {"documents": documents, "base_url": base_url, "receive_url": receive_url},
            request=request,
        )
    )
