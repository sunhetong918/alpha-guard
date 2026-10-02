"""Explicit research commands. Listing and configuration never start collection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="按需新闻与博主研究；默认关闭自动跟踪。", no_args_is_help=True)


def _service():
    from main import STATE_PATH
    from .service import ResearchService

    return ResearchService(STATE_PATH)


def _print(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.command("status")
def status(page: Annotated[int, typer.Option(min=0, help="本地快照页码，从0开始")] = 0) -> None:
    """读取本地证据、报告与配置；不联网。CLI会话独立于桌面监控。"""
    value = _service().snapshot(page=page)
    value["runtime"]["scope"] = "cli_session; desktop status is available in the research page"
    _print(value)


@app.command("configure")
def configure(path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """保存研究设置JSON；不会启动采集或自动跟踪。"""
    try:
        if path.stat().st_size > 64_000:
            raise ValueError("settings too large")
        _print(_service().configure(json.loads(path.read_text(encoding="utf-8"))))
    except (ValueError, TypeError, OSError):
        typer.echo("研究配置无效；请检查市场、周期、证券和频率。", err=True)
        raise typer.Exit(2) from None


@app.command("subscribe")
def subscribe(
    platform: str,
    url: str,
    label: str = "",
    feed_url: Annotated[str | None, typer.Option(help="合法可用的作者 RSS/Atom 来源")] = None,
) -> None:
    """保存指定作者；添加订阅不会启动监控。"""
    try:
        _print(_service().add_subscription({
            "platform": platform, "url": url, "label": label,
            "feed_url": feed_url, "enabled": True,
        }))
    except (ValueError, TypeError):
        typer.echo("订阅无效；请检查平台、主页或feed地址。", err=True)
        raise typer.Exit(2) from None


@app.command("subscription")
def subscription(identity: str, enabled: bool = True, delete: bool = False) -> None:
    """启停或删除订阅，保留历史证据。"""
    try:
        _service().edit_subscription(identity, enabled=enabled, delete=delete)
    except ValueError:
        typer.echo("找不到订阅或参数无效。", err=True)
        raise typer.Exit(2) from None


@app.command("import")
def import_material(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    source_url: Annotated[str, typer.Option(help="可核验的原文/视频地址")] = "",
    kind: str = "article",
    title: str = "",
    author: str = "",
    published_at: Annotated[str | None, typer.Option(help="原始发布时间，带时区 ISO8601")] = None,
) -> None:
    """导入正文/SRT/VTT；保存后显式scan才会分析。"""
    try:
        if path.stat().st_size > 750_000:
            raise ValueError("material too large")
        item = _service().import_text({
            "text": path.read_text(encoding="utf-8-sig"), "kind": kind,
            "source_url": source_url, "title": title or path.stem,
            "author": author, "published_at": published_at,
        })
        _print({key: item.get(key) for key in ("id", "title", "content_kind", "completeness", "quality_issues")})
    except (ValueError, OSError):
        typer.echo("材料无效；请检查大小、编码、原文地址或字幕时间戳。", err=True)
        raise typer.Exit(2) from None


def _run(*, continuous: bool = False, media: dict | None = None) -> None:
    service = _service()
    try:
        service.launch(continuous=continuous, media=media)
        if continuous:
            typer.echo("研究自动跟踪已由你开启；Ctrl+C 停止，本进程退出后不自动恢复。")
        service.wait()
    except ValueError:
        typer.echo("研究已在其他会话运行，或设置无效；请先停止正在运行的跟踪。", err=True)
        raise typer.Exit(2) from None
    except KeyboardInterrupt:
        service.stop()
    finally:
        service.close()
    result = service.snapshot()["runtime"]
    _print(result)
    if (result.get("last_run") or {}).get("status") in {"failed", "needs_configuration"}:
        raise typer.Exit(1)


@app.command("scan")
def scan() -> None:
    """执行一轮采集/分析后结束，不创建定时任务。"""
    _run()


@app.command("watch")
def watch() -> None:
    """显式前台跟踪，按保存的频率执行；退出即停止。"""
    _run(continuous=True)


@app.command("transcribe")
def transcribe(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    source_url: str = "",
    title: str = "",
) -> None:
    """转写本地授权音视频；先配置ALPHA_GUARD_TRANSCRIPTION_COMMAND。"""
    _run(media={"path": str(path.resolve()), "source_url": source_url, "title": title or path.stem})


@app.command("show")
def show(kind: str, identity: str) -> None:
    """离线读取 evidence、report 或 digest 的完整内容。"""
    try:
        _print(_service().detail(kind, identity, full=True))
    except ValueError:
        typer.echo("找不到研究记录。", err=True)
        raise typer.Exit(2) from None


@app.command("models")
def models() -> None:
    """列出模型 profile；不显示密钥，不联网。"""
    _print({"profiles": _service().list_model_profiles()})


@app.command("model-save")
def model_save(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    api_key: Annotated[str | None, typer.Option("--api-key", help="新API密钥；省略则保留已存密钥")] = None,
) -> None:
    """保存模型 profile JSON；密钥只进钥匙串，不会写入研究库。"""
    try:
        if path.stat().st_size > 64_000:
            raise ValueError("profile too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
        _print(_service().save_model_profile({"profile": payload, "api_key": api_key}))
    except (ValueError, TypeError, OSError):
        typer.echo("模型 profile 无效；请检查协议、端点、模型名和私网开关。", err=True)
        raise typer.Exit(2) from None


@app.command("model-activate")
def model_activate(identity: Annotated[str | None, typer.Argument(help="profile id；留空取消激活")] = None) -> None:
    """激活模型 profile；留空参数回退环境变量提供者。"""
    try:
        _print(_service().activate_model_profile(identity or None))
    except ValueError:
        typer.echo("找不到可用的模型 profile，或研究正在运行。", err=True)
        raise typer.Exit(2) from None


@app.command("model-delete")
def model_delete(identity: str) -> None:
    """删除模型 profile 并清理其密钥。"""
    try:
        _service().delete_model_profile(identity)
        typer.echo(f"已删除 {identity}。")
    except ValueError:
        typer.echo("找不到模型 profile。", err=True)
        raise typer.Exit(2) from None


@app.command("model-test")
def model_test(identity: str) -> None:
    """对模型 profile 发起一次极小真实请求验证连通；会产生一次调用费用。"""
    typer.echo("即将对该 profile 发起一次真实模型请求（可能产生费用）。", err=True)
    _print(_service().test_model_profile(identity))


@app.command("digest-status")
def digest_status() -> None:
    """读取每日摘要设置、计划与最近记录；不联网。"""
    from .digest import digest_snapshot

    value = digest_snapshot(_service().path)
    value["runtime"]["scope"] = "cli_session; scheduler runs inside Guardian"
    _print(value)


@app.command("digest-configure")
def digest_configure(path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """保存每日摘要设置JSON；开启后由 Guardian 按计划执行。"""
    try:
        if path.stat().st_size > 64_000:
            raise ValueError("settings too large")
        _print(_service().save_digest_settings(json.loads(path.read_text(encoding="utf-8"))))
    except (ValueError, TypeError, OSError):
        typer.echo("摘要设置无效；请检查时刻、主题和开关。", err=True)
        raise typer.Exit(2) from None


@app.command("digest-run")
def digest_run() -> None:
    """立即生成一轮摘要；不改变已设定的每日计划。"""
    from .digest import submit_manual_digest

    receipt = submit_manual_digest(_service().path)
    _print(receipt)
    if not receipt.get("accepted"):
        raise typer.Exit(1)


@app.command("digest-show")
def digest_show(identity: str) -> None:
    """离线读取一条摘要的完整内容。"""
    try:
        _print(_service().detail("digest", identity, full=True))
    except ValueError:
        typer.echo("找不到摘要记录。", err=True)
        raise typer.Exit(2) from None


@app.command("digest-history")
def digest_history(offset: Annotated[int, typer.Option(min=0)] = 0,
                   limit: Annotated[int, typer.Option(min=1, max=100)] = 20) -> None:
    """分页读取摘要历史索引，不联网。"""
    from .store import ResearchStore

    with ResearchStore(_service().path) as store:
        records = store.list_digests(limit=limit, offset=offset)
    _print({"offset": offset, "items": [
        {key: record.get(key) for key in ("id", "created_at", "trigger", "status", "detail", "counts")}
        for record in records]})


@app.command('replay')
def replay(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    as_of: Annotated[str, typer.Option(help='跟踪截止时刻，含时区的 ISO8601')],
) -> None:
    """离线跟踪历史报告与后续价格；无前视输入，不是交易回测。"""
    from .models import parse_time
    from .replay import track_reports
    from .store import ResearchStore

    try:
        cutoff = parse_time(as_of)
        if cutoff is None or path.stat().st_size > 5_000_000:
            raise ValueError('invalid tracking input')
        samples = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(samples, list):
            raise ValueError('expected a price observation list')
        with ResearchStore(_service().path) as store:
            _print(track_reports(store, samples, as_of=cutoff))
    except (ValueError, TypeError, OSError):
        typer.echo('跟踪输入无效；请检查证券、价格、来源及带时区的两个时间戳。', err=True)
        raise typer.Exit(2) from None


@app.command("history")
def history(kind: str, offset: Annotated[int, typer.Option(min=0)] = 0,
            limit: Annotated[int, typer.Option(min=1, max=100)] = 20) -> None:
    """分页读取 evidence/report 全部历史索引，不联网。"""
    from .store import ResearchStore

    if kind not in {"evidence", "report"}:
        typer.echo("类型应为 evidence 或 report。", err=True)
        raise typer.Exit(2)
    with ResearchStore(_service().path) as store:
        records = store.list_evidence(limit=limit, offset=offset) if kind == "evidence" else store.list_reports(limit=limit, offset=offset)
    fields = ("id", "title", "source_url", "published_at", "content_kind", "completeness") if kind == "evidence" else ("id", "instrument", "horizon", "status", "action", "created_at", "valid_until")
    _print({"kind": kind, "offset": offset, "items": [
        {key: item.model_dump(mode="json").get(key) for key in fields} for item in records]})
