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
        "SELECT showboat_id, markdown FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    rows = result.rows
    assert len(rows) == 1
    assert rows[0][0] == "abc-123"
    assert rows[0][1] == "# My Demo"


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
        "SELECT markdown FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    assert result.rows[0][0] == "Some **bold** text"


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
        "SELECT markdown FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    markdown = result.rows[0][0]
    assert "```bash" in markdown
    assert "echo hello" in markdown
    assert "```output" in markdown
    assert "hello" in markdown


@pytest.mark.asyncio
async def test_receive_exec_with_backticks():
    """Exec where the code/output contains backticks should use longer fences."""
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

    db = datasette.get_internal_database()
    result = await db.execute(
        "SELECT markdown FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    markdown = result.rows[0][0]
    # Fences should be longer than 3 backticks since content contains ```
    assert "````" in markdown


@pytest.mark.asyncio
async def test_receive_image():
    datasette = Datasette(memory=True)
    fake_png = b"\x89PNG\r\n\x1a\nfake image data"
    response = await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "abc-123", "command": "image", "input": "screenshot.py", "alt": "A screenshot"},
        files={"image": ("screenshot.png", fake_png, "image/png")},
    )
    assert response.status_code == 201

    db = datasette.get_internal_database()
    result = await db.execute(
        "SELECT markdown, image FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    row = result.rows[0]
    assert "screenshot.py" in row[0]
    assert "{image}" in row[0]
    assert "![A screenshot]()" in row[0]
    assert row[1] == fake_png


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

    # Pop the last chunk
    response = await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "abc-123", "command": "pop"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True

    result = await db.execute(
        "SELECT COUNT(*) FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    assert result.rows[0][0] == 1

    # The remaining chunk should be the init, not the note
    result = await db.execute(
        "SELECT markdown FROM showboat_chunks WHERE showboat_id = ?",
        ["abc-123"],
    )
    assert result.rows[0][0] == "# Title"


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
    assert data["chunks"][0]["markdown"] == "# Title"
    assert data["chunks"][1]["markdown"] == "Hello world"
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

    fake_png = b"fake-png-data"
    await datasette.client.post(
        "/-/showboat/receive",
        data={"uuid": "doc-1", "command": "image", "input": "shot.py", "alt": "test"},
        files={"image": ("test.png", fake_png, "image/png")},
    )

    response = await datasette.client.get("/-/showboat/doc-1.json")
    data = response.json()
    assert len(data["chunks"]) == 1
    assert "image" in data["chunks"][0]
    assert base64.b64decode(data["chunks"][0]["image"]) == fake_png


@pytest.mark.asyncio
async def test_document_viewer_page():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/showboat/abc-def-123")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "abc-def-123" in response.text
    assert "marked.min.js" in response.text


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
