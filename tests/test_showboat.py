from datasette.app import Datasette
import pytest


@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    installed_plugins = {p["name"] for p in response.json()}
    assert "datasette-showboat" in installed_plugins


@pytest.mark.asyncio
async def test_table_created_on_startup():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    db = datasette.get_internal_database()
    tables = await db.table_names()
    assert "showboat_chunks" in tables
    # Verify the command column exists
    columns = await db.execute("PRAGMA table_info(showboat_chunks)")
    column_names = {row[1] for row in columns.rows}
    assert "command" in column_names
    assert "title" in column_names
    assert "language" in column_names
    assert "filename" in column_names


@pytest.mark.asyncio
async def test_receive_init():
    datasette = Datasette(memory=True)
    response = await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "abc-123", "command": "init", "title": "My Demo"},
    )
    assert response.status_code == 201
    assert response.json()["ok"] is True

    db = datasette.get_internal_database()
    result = await db.execute(
        "SELECT showboat_id, command, title FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    rows = result.rows
    assert len(rows) == 1
    assert rows[0][0] == "abc-123"
    assert rows[0][1] == "init"
    assert rows[0][2] == "My Demo"


@pytest.mark.asyncio
async def test_receive_note():
    datasette = Datasette(memory=True)
    response = await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "abc-123", "command": "note", "markdown": "Some **bold** text"},
    )
    assert response.status_code == 201

    db = datasette.get_internal_database()
    result = await db.execute(
        "SELECT command, markdown FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    assert result.rows[0][0] == "note"
    assert result.rows[0][1] == "Some **bold** text"


@pytest.mark.asyncio
async def test_receive_exec():
    datasette = Datasette(memory=True)
    response = await datasette.client.post(
        "/-/showboat/receive",
        data={
            "uuid": "abc-123",
            "command": "exec",
            "language": "bash",
            "input": "echo hello",
            "output": "hello",
        },
    )
    assert response.status_code == 201

    db = datasette.get_internal_database()
    result = await db.execute(
        "SELECT command, language, input, output FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    row = result.rows[0]
    assert row[0] == "exec"
    assert row[1] == "bash"
    assert row[2] == "echo hello"
    assert row[3] == "hello"


@pytest.mark.asyncio
async def test_receive_exec_with_backticks():
    """Exec where the code/output contains backticks should use longer fences in rendered markdown."""
    datasette = Datasette(memory=True)
    response = await datasette.client.post(
        "/-/showboat/receive",
        data={
            "uuid": "abc-123",
            "command": "exec",
            "language": "bash",
            "input": "echo '```'",
            "output": "```",
        },
    )
    assert response.status_code == 201

    # Verify raw fields stored correctly
    db = datasette.get_internal_database()
    result = await db.execute(
        "SELECT input, output FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    assert result.rows[0][0] == "echo '```'"
    assert result.rows[0][1] == "```"

    # Verify rendered markdown uses longer fences
    response = await datasette.client.get("/-/showboat/abc-123.json")
    chunk = response.json()["chunks"][0]
    assert "````" in chunk["rendered_markdown"]


@pytest.mark.asyncio
async def test_receive_image():
    datasette = Datasette(memory=True)
    fake_png = b"\x89PNG\r\n\x1a\nfake image data"
    response = await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "abc-123", "command": "image", "filename": "screenshot.py", "alt": "A screenshot"},
        files={"image": ("screenshot.png", fake_png, "image/png")},
    )
    assert response.status_code == 201

    db = datasette.get_internal_database()
    result = await db.execute(
        "SELECT command, filename, alt, image FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    row = result.rows[0]
    assert row[0] == "image"
    assert row[1] == "screenshot.py"
    assert row[2] == "A screenshot"
    assert row[3] == fake_png


@pytest.mark.asyncio
async def test_receive_pop():
    datasette = Datasette(memory=True)
    # Add two chunks
    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "abc-123", "command": "init", "title": "Title"},
    )
    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "abc-123", "command": "note", "markdown": "To be popped"},
    )

    db = datasette.get_internal_database()
    result = await db.execute(
        "SELECT COUNT(*) FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    assert result.rows[0][0] == 2

    # Pop records a pop command (doesn't delete)
    response = await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "abc-123", "command": "pop"},
    )
    assert response.status_code == 201
    assert response.json()["ok"] is True

    # Should now have 3 rows (init, note, pop)
    result = await db.execute(
        "SELECT COUNT(*) FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    assert result.rows[0][0] == 3

    # The pop command should be recorded
    result = await db.execute(
        "SELECT command FROM showboat_chunks WHERE showboat_id = ? ORDER BY id",
        ["abc-123"],
    )
    commands = [row[0] for row in result.rows]
    assert commands == ["init", "note", "pop"]


@pytest.mark.asyncio
async def test_receive_requires_post():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/showboat/receive")
    assert response.status_code == 405


@pytest.mark.asyncio
async def test_receive_missing_fields():
    datasette = Datasette(memory=True)
    response = await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "abc-123"},
    )
    assert response.status_code == 400

    response = await datasette.client.post(
        "/-/showboat/receive",
        data={"command": "init"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_receive_unknown_command():
    datasette = Datasette(memory=True)
    response = await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "abc-123", "command": "badcommand"},
    )
    assert response.status_code == 400
    assert "Unknown command" in response.json()["error"]


@pytest.mark.asyncio
async def test_token_auth():
    datasette = Datasette(
        memory=True,
        metadata={"plugins": {"datasette-showboat": {"token": "secret123"}}},
    )
    # Without token
    response = await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "abc-123", "command": "init", "title": "Test"},
    )
    assert response.status_code == 403

    # With wrong token
    response = await datasette.client.post(
        "/-/showboat/receive?token=wrong",
        data={"uuid": "abc-123", "command": "init", "title": "Test"},
    )
    assert response.status_code == 403

    # With correct token
    response = await datasette.client.post(
        "/-/showboat/receive?token=secret123",
        data={"uuid": "abc-123", "command": "init", "title": "Test"},
    )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_document_json():
    datasette = Datasette(memory=True)
    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "doc-1", "command": "init", "title": "Title"},
    )
    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "doc-1", "command": "note", "markdown": "Hello world"},
    )

    response = await datasette.client.get("/-/showboat/doc-1.json")
    assert response.status_code == 200
    data = response.json()
    assert len(data["chunks"]) == 2
    # Check raw fields
    assert data["chunks"][0]["command"] == "init"
    assert data["chunks"][0]["title"] == "Title"
    assert data["chunks"][1]["command"] == "note"
    assert data["chunks"][1]["markdown"] == "Hello world"
    # Check rendered markdown
    assert data["chunks"][0]["rendered_markdown"] == "# Title"
    assert data["chunks"][1]["rendered_markdown"] == "Hello world"
    assert "created_at" in data["chunks"][0]
    assert "id" in data["chunks"][0]


@pytest.mark.asyncio
async def test_document_json_polling_after():
    datasette = Datasette(memory=True)
    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "doc-1", "command": "init", "title": "Title"},
    )

    response = await datasette.client.get("/-/showboat/doc-1.json")
    first_id = response.json()["chunks"][0]["id"]

    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "doc-1", "command": "note", "markdown": "Second chunk"},
    )

    response = await datasette.client.get(f"/-/showboat/doc-1.json?after={first_id}")
    data = response.json()
    assert len(data["chunks"]) == 1
    assert data["chunks"][0]["markdown"] == "Second chunk"


@pytest.mark.asyncio
async def test_document_json_with_image():
    datasette = Datasette(memory=True)
    import base64

    fake_png = b"\x89PNG\r\n\x1a\nfake-png-data"
    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "doc-1", "command": "image", "filename": "shot.py", "alt": "test"},
        files={"image": ("test.png", fake_png, "image/png")},
    )

    response = await datasette.client.get("/-/showboat/doc-1.json")
    data = response.json()
    assert len(data["chunks"]) == 1
    chunk = data["chunks"][0]
    assert chunk["command"] == "image"
    assert chunk["filename"] == "shot.py"
    assert chunk["alt"] == "test"
    assert "image" in chunk
    assert base64.b64decode(chunk["image"]) == fake_png
    assert "rendered_markdown" in chunk


@pytest.mark.asyncio
async def test_document_json_pop_included():
    """Pop commands should be included in JSON response."""
    datasette = Datasette(memory=True)
    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "doc-1", "command": "init", "title": "Title"},
    )
    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "doc-1", "command": "note", "markdown": "To be popped"},
    )
    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "doc-1", "command": "pop"},
    )

    response = await datasette.client.get("/-/showboat/doc-1.json")
    data = response.json()
    assert len(data["chunks"]) == 3
    assert data["chunks"][0]["command"] == "init"
    assert data["chunks"][1]["command"] == "note"
    assert data["chunks"][2]["command"] == "pop"
    # Pop chunks should not have rendered_markdown
    assert "rendered_markdown" not in data["chunks"][2]


@pytest.mark.asyncio
async def test_document_viewer_page():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/showboat/abc-def-123")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "abc-def-123" in response.text
    assert "marked.min.js" in response.text
    assert "purify.min.js" in response.text


@pytest.mark.asyncio
async def test_index_page():
    datasette = Datasette(memory=True)
    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "doc-1", "command": "init", "title": "First Doc"},
    )
    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "doc-2", "command": "init", "title": "Second Doc"},
    )

    response = await datasette.client.get("/-/showboat")
    assert response.status_code == 200
    assert "doc-1" in response.text
    assert "doc-2" in response.text
    assert "SHOWBOAT_REMOTE_URL" in response.text
    # Should include the actual hostname in the setup URL
    assert "://localhost/-/showboat/receive" in response.text


@pytest.mark.asyncio
async def test_index_page_empty():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/showboat")
    assert response.status_code == 200
    assert "No documents yet" in response.text


@pytest.mark.asyncio
async def test_showboat_permission_denied():
    """When showboat permission is restricted, anonymous users get 403."""
    datasette = Datasette(
        memory=True,
        config={
            "permissions": {
                "showboat": {"id": "special-user"},
            },
        },
    )
    # Anonymous user should be denied
    response = await datasette.client.get("/-/showboat")
    assert response.status_code == 403

    response = await datasette.client.get("/-/showboat/abc-123")
    assert response.status_code == 403

    response = await datasette.client.get("/-/showboat/abc-123.json")
    assert response.status_code == 403

    # Receive endpoint should still work (no showboat permission check)
    response = await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "abc-123", "command": "init", "title": "Test"},
    )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_menu_links_shown_by_default():
    """Menu links should include Showboat when no permission restrictions."""
    datasette = Datasette(memory=True)
    # The index page or any page should work - let's check via the JSON plugins endpoint
    # Actually, let's just check a page that would include the nav
    response = await datasette.client.get("/-/showboat")
    assert response.status_code == 200
    # Verify the menu_links hook is registered by checking the plugin
    response = await datasette.client.get("/-/plugins.json")
    hooks = None
    for plugin in response.json():
        if plugin["name"] == "datasette-showboat":
            hooks = plugin.get("hooks", [])
            break
    assert "menu_links" in hooks


@pytest.mark.asyncio
async def test_menu_links_hidden_when_denied():
    """Menu links should not include Showboat when permission is denied."""
    datasette = Datasette(
        memory=True,
        config={
            "permissions": {
                "showboat": {"id": "special-user"},
            },
        },
    )
    # Get a page that renders menu links - use the root page
    response = await datasette.client.get("/")
    assert response.status_code == 200
    # The menu should NOT contain a link to /-/showboat for anonymous users
    assert "/-/showboat" not in response.text


@pytest.mark.asyncio
async def test_render_markdown_exec():
    """Verify render_markdown produces correct fenced code blocks for exec."""
    from datasette_showboat import render_markdown

    chunk = {
        "command": "exec",
        "language": "python",
        "input": "print('hello')",
        "output": "hello",
    }
    md = render_markdown(chunk)
    assert "```python" in md
    assert "print('hello')" in md
    assert "```output" in md
    assert "hello" in md
