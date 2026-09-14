from preflight import CHECK_GUIDES, checks, describe_checks


def test_checks_distinguish_platform_credentials_and_real_model_files(tmp_path, monkeypatch):
    import desensitize
    monkeypatch.setenv("SCPMA_COOKIE", "synthetic")
    monkeypatch.setenv("SCPMA_AUTH_TOKEN", "synthetic")
    monkeypatch.setenv("APP_TOKEN", "")
    monkeypatch.setattr(desensitize, "_PADDLE_MODEL_DIR", str(tmp_path))
    values = checks(tmp_path)
    assert values["平台 Cookie 和 Authorization（网页粘贴；未验证有效性）"]
    assert not values["平台 APP_TOKEN（asca 请求凭证；未验证有效性）"]
    assert not values["Paddle 三组模型文件"]
    for part in ("det", "rec", "cls"):
        (tmp_path / part).mkdir()
        for name in ("inference.pdmodel", "inference.pdiparams"):
            (tmp_path / part / name).write_bytes(b"synthetic")
    assert checks(tmp_path)["Paddle 三组模型文件"]


def test_optional_platform_fields_have_guidance_but_do_not_block_core(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "")
    monkeypatch.setenv("AGENT_ID", "")
    monkeypatch.setenv("CODE", "unused-synthetic")
    values = checks(tmp_path)
    rows = describe_checks(values)
    assert set(values) == set(CHECK_GUIDES)
    assert not any("CODE" in row["label"] for row in rows)
    by_label = {row["label"]: row for row in rows}
    assert by_label["平台 APP_TOKEN（asca 请求凭证；未验证有效性）"]["kind"] == "optional"
    assert by_label["平台 AGENT_ID（审批页参数）"]["kind"] == "optional"
    assert all(len(row["steps"]) >= 2 for row in rows)


def test_preflight_page_shows_instructions_without_credential_values(monkeypatch):
    from fastapi.testclient import TestClient

    from web.app import app

    monkeypatch.setenv("WEB_AUTH_MODE", "local")
    monkeypatch.setenv("SCPMA_COOKIE", "session=private-synthetic")
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
        response = client.get("/preflight")
    assert response.status_code == 200
    assert response.text.count("如何配置") == len(CHECK_GUIDES)
    assert "按需配置" in response.text
    assert "session=private-synthetic" not in response.text
