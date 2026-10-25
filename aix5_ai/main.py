from __future__ import annotations
import asyncio, datetime, logging, os, time, uuid, csv, io, json, re
import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from .config import CAPTURE_BIN, EVENTS, HOST, PORT, MODELS, DEFAULT_SETTINGS, model_confidence_threshold
from . import codec_tuning
from .hw_capture import manager, make_web_preview
from .inference import (
    get_detector,
    hbm_runtime,
    prepare_inference_process,
    infer_in_process,
)
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from .labels import COCO_GROUPS, COCO_LABELS, COCO_LABELS_ZH, DEFAULT_ENABLED_LABELS
from .metrics import metrics
from .onvif import discover, probe
from .onvif_auth import authenticate as authenticate_onvif, OnvifAuthError
from .onvif_gateway import (
    gateway as onvif_gateway,
    _action_name,
    _fault,
    _local_ipv4,
)
from .onvif_events import OnvifEventHub
from .rtsp_output import RtspOutputManager
from .rules import RuleEngine
from .schemas import (
    CameraConfig,
    CameraPatch,
    CameraLabels,
    CameraOrder,
    Login,
    PasswordChange,
    AccountChange,
    ONVIFProbe,
    RuleConfig,
    RulePatch,
    SettingsConfig,
    SettingsPatch,
)
from pydantic import ValidationError
from .store import Store
from .web_ui import INDEX as WEB_INDEX
from .webrtc import create_offer_answer, close_peers, peer_connections, PeerLimitError

logging.basicConfig(
    level=os.getenv("AIX5_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("aix5")


@asynccontextmanager
async def lifespan(app):
    try:
        await startup()
        yield
    finally:
        await shutdown()


app = FastAPI(title="AIX5 AI 视频分析", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
store = None
tasks = {}
engine = RuleEngine()
inference_sem = asyncio.Semaphore(1)
inference_executor = None
camera_lock = asyncio.Lock()
auth_lock = asyncio.Lock()
settings_lock = asyncio.Lock()
onvif_events = OnvifEventHub()
rtsp_output = None


async def auth(session: str | None = Cookie(default=None, alias="aix5_session")):
    if not store or not store.valid_session(session):
        raise HTTPException(401, "请先登录")
    return True


def camera_view(c):
    view = {
        k: c.get(k)
        for k in (
            "id",
            "name",
            "rtsp_url",
            "source_type",
            "transport",
            "enabled",
            "status",
            "last_error",
            "last_frame_at",
            "width",
            "height",
            "codec",
            "decode_backend",
            "encode_backend",
            "encode_error",
            "video_encoded_frames",
            "video_encode_ms",
            "video_encode_work_ms",
            "rtsp_output_status",
            "rtsp_output_error",
            "rtsp_video_packets",
            "rtsp_audio_packets",
            "rtsp_audio_pcm_chunks",
            "rtsp_audio_pcm_samples",
            "rtsp_audio_dropped_chunks",
            "rtsp_audio_source_sequence",
            "rtsp_mux_queued_frames",
            "python_frame_copies",
            "audio_decode_starts",
            "audio_read_timeouts",
            "audio_decoded_chunks",
            "audio_decoded_samples",
            "decode_frames",
            "decode_fps",
            "source_fps",
            "audio_present",
            "audio_status",
            "audio_codec",
            "audio_sample_rate",
            "audio_channels",
            "audio_level",
            "audio_dbfs",
            "audio_error",
            "preview_frames",
            "inference_frames",
            "last_inference_at",
            "detection_count",
            "inference_latency_ms",
            "inference_error",
            "model_id",
            "analysis_fps",
            "preview_fps",
        )
    }
    view["enabled_labels"] = c.get("enabled_labels", DEFAULT_ENABLED_LABELS)
    return view


def redact_url(u):
    """Strip user:password from an RTSP/URL so credentials never leave the box."""
    try:
        parts = urlsplit(u or "")
        if parts.username or parts.password:
            host = parts.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            if parts.port:
                host = f"{host}:{parts.port}"
            return urlunsplit(
                (parts.scheme, host, parts.path, parts.query, parts.fragment)
            )
    except Exception:
        return "[无效 URL]"
    return u


async def run_worker(func, *args):
    """Cancellation must not release a BPU/DB resource before its thread exits."""
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


def camera_state(camera):
    cid = camera["id"]
    if camera.get("status") != "running":
        engine.reset_camera(cid)
        camera["present"] = {}
        # A disconnected/disabled source must not leave a stale ONVIF binary
        # sensor in the ``true`` state.
        onvif_events.clear_camera(cid)
def refresh_discovery():
    settings = store.get("settings")
    settings = _reconcile_saved_base_url(settings)
    # Keep ONVIF profile enumeration in sync when the manager dictionary is
    # replaced by a reorder operation or changed by add/delete operations.
    configure_onvif_gateway(settings["base_url"])


def configure_onvif_gateway(base_url=None):
    base_url = base_url or store.get("settings")["base_url"]
    host = urlsplit(base_url).hostname or "127.0.0.1"
    onvif_gateway.configure(
        manager.cameras,
        store.rules,
        base_url,
        event_hub=onvif_events,
        device_id=store.get("device_id"),
        stream_uri=(lambda camera: rtsp_output.stream_uri(camera["id"], host))
        if rtsp_output else None,
        media_profile=rtsp_output.media_profile if rtsp_output else None,
    )


def _reconcile_saved_base_url(settings=None):
    """Replace a stale private IPv4 address after a DHCP/interface change.

    A configured DNS name or public/NAT address is intentional and remains
    untouched.  Only private, loopback, or link-local numeric addresses are
    reconciled with the active LAN interface selected by the kernel.
    """

    settings = dict(settings or store.get("settings", {}))
    configured = str(settings.get("base_url") or "").rstrip("/")
    try:
        parts = urlsplit(configured)
        address = ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        return settings
    if address.version != 4 or not (
        address.is_private or address.is_loopback or address.is_link_local
    ):
        return settings
    current = _local_ipv4()
    if not current or current == parts.hostname:
        return settings
    netloc = current + (f":{parts.port}" if parts.port else "")
    updated = urlunsplit(
        (parts.scheme or "http", netloc, parts.path, parts.query, parts.fragment)
    ).rstrip("/")
    settings["base_url"] = updated
    store.set("settings", settings)
    return settings


def _request_public_base(request: Request):
    """Return the origin the current client actually used.

    Direct LAN requests are authoritative for the advertised ONVIF XAddr and
    RTSP host.  Test clients and loopback health checks fall back to the saved
    public origin so local control-plane tests do not rewrite production data.
    """

    candidate = str(request.base_url).rstrip("/")
    host = urlsplit(candidate).hostname or ""
    if host in {"test", "testserver", "localhost", "127.0.0.1", "::1"}:
        return _reconcile_saved_base_url().get("base_url") or candidate
    try:
        candidate_ip = ipaddress.ip_address(host)
    except ValueError:
        candidate_ip = None
    if candidate_ip is not None and (
        candidate_ip.is_private or candidate_ip.is_loopback or candidate_ip.is_link_local
    ):
        current = _local_ipv4()
        if current and host != current:
            return _reconcile_saved_base_url().get("base_url") or candidate
    return candidate


def _sync_request_public_base(request: Request):
    settings = _reconcile_saved_base_url()
    candidate = _request_public_base(request)
    if candidate != settings.get("base_url"):
        settings["base_url"] = candidate
        store.set("settings", settings)
        configure_onvif_gateway(candidate)
    return candidate


async def refresh_rtsp_credentials():
    if rtsp_output is not None:
        password = await run_worker(store.onvif_password)
        await rtsp_output.configure(store.get("username"), password)


async def inference_loop(cid):
    camera = manager.cameras[cid]
    queue = manager.frame_queues[cid]
    last_infer = -1e20
    while True:
        bgr, jpg, generation = await queue.get()
        if not camera.get("enabled", True) or camera.get("status") != "running":
            continue
        fps = float(camera.get("analysis_fps", 5))
        if camera.get("decode_backend") == "software":
            fps = min(fps, 1.5)
        # Wait for the next slot instead of dropping a frame already sampled
        # at analysis_fps by capture. Two independent drop gates halve AI FPS.
        delay = last_infer + 1 / fps - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        model_id = camera.get("model_id", "yolo11n")
        try:
            async with inference_sem:
                # Consume the freshest frame after waiting for the shared BPU.
                while not queue.empty():
                    bgr, jpg, generation = queue.get_nowait()
                if (
                    generation != manager.generations.get(cid)
                    or camera.get("status") != "running"
                ):
                    continue
                last_infer = time.monotonic()
                if inference_executor is None:
                    result = await run_worker(lambda: get_detector(model_id).infer(bgr))
                else:
                    prepared = await run_worker(prepare_inference_process, bgr)
                    future = asyncio.get_running_loop().run_in_executor(
                        inference_executor,
                        infer_in_process,
                        model_id,
                        prepared,
                        bgr.shape[1],
                        bgr.shape[0],
                    )
                    try:
                        result = await asyncio.shield(future)
                    except asyncio.CancelledError:
                        await asyncio.gather(future, return_exceptions=True)
                        raise
            if (
                generation != manager.generations.get(cid)
                or model_id != camera.get("model_id", "yolo11n")
                or not camera.get("enabled", True)
                or camera.get("status") != "running"
            ):
                continue
            enabled = set(camera.get("enabled_labels", DEFAULT_ENABLED_LABELS))
            detections = []
            for detection in result.get("detections", []):
                class_id = detection["class_id"]
                label = (
                    COCO_LABELS[class_id]
                    if 0 <= class_id < len(COCO_LABELS)
                    else f"class_{class_id}"
                )
                if label in enabled:
                    detections.append(
                        {
                            **detection,
                            "label": label,
                            "label_zh": COCO_LABELS_ZH.get(label, label),
                        }
                    )
            result["detections"] = detections
            height, width = bgr.shape[:2]
            camera.update(
                last_inference=result,
                inference_error=None,
                inference_frames=camera.get("inference_frames", 0) + 1,
                last_inference_at=datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),
                inference_latency_ms=result.get("latency_ms"),
                detection_count=len(detections),
                detection_payload={
                    "camera_id": cid,
                    "width": width,
                    "height": height,
                    "detections": detections,
                },
            )
            evaluated_rules = {rule["id"]: rule for rule in store.rules(cid)}
            events, present = engine.evaluate(
                cid,
                detections,
                list(evaluated_rules.values()),
                time.time(),
                image_size=(width, height),
            )
            camera["present"] = present
            camera_state(camera)
            if not events:
                onvif_events.sync_active(cid, engine.active_rules(cid))
                continue
            # Encode the frame that produced these detections, not a newer JPEG cache.
            image = jpg or await run_worker(make_web_preview, bgr)
            if not image:
                raise RuntimeError("事件截图编码失败")
            if generation != manager.generations.get(cid):
                continue
            settings = _reconcile_saved_base_url(store.get("settings"))
            base = settings.get("base_url", "").rstrip("/")

            def rule_event_is_current(event):
                key = (cid, event["rule_id"], event["target_type"])
                current = next(
                    (rule for rule in store.rules(cid) if rule["id"] == event["rule_id"]),
                    None,
                )
                original = evaluated_rules.get(event["rule_id"])
                return (
                    key in engine.active_rules(cid)
                    and current is not None
                    and original is not None
                    and RuleConfig.model_validate(current).model_dump(exclude={"name"})
                    == RuleConfig.model_validate(original).model_dump(exclude={"name"})
                )

            for event in events:
                if not rule_event_is_current(event):
                    continue
                eid = str(uuid.uuid4())
                payload = {
                    "id": eid,
                    "camera_id": cid,
                    "camera_name": camera["name"],
                    "rule_id": event["rule_id"],
                    "rule_name": event["rule_name"],
                    "event_type": event["event_type"],
                    "target_type": event["target_type"],
                    "confidence": event["confidence"],
                    "bbox": event["bbox"],
                    "timestamp": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "screenshot_url": f"{base}/api/events/{eid}/image?token={store.get('ha_token')}",
                    "created": time.time(),
                    "image_path": str(EVENTS / f"{eid}.jpg"),
                    "size": len(image),
                }
                await run_worker(store.insert_event, payload, image)
                # Persistence yields to rule edits/deletion. Never publish an
                # old-condition trigger into the newly configured rule sensor.
                if rule_event_is_current(event):
                    onvif_events.publish_trigger(cid, {**event, "event_id": eid})
            # One sensor aggregates a rule's targets. Publish newly triggered
            # targets first so a same-frame target handoff cannot pulse false.
            onvif_events.sync_active(cid, engine.active_rules(cid))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            camera["inference_error"] = str(exc)
            camera.update(
                detection_count=0,
                present={},
                detection_payload={"camera_id": cid, "detections": []},
            )
            engine.reset_camera(cid)
            onvif_events.clear_camera(cid)
            camera_state(camera)
            log.exception("inference camera=%s", cid)


async def startup():
    global store, inference_executor, rtsp_output
    onvif_events.reset()
    if hbm_runtime is not None:
        inference_executor = ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        )
    store = await run_worker(Store)
    rtsp_output = RtspOutputManager(manager, store.root)
    manager.status_listener = camera_state
    settings = _reconcile_saved_base_url(store.get("settings"))
    manager.preview_resolution = settings.get("preview_resolution", "960x540")
    manager.allow_software_fallback = settings.get("allow_software_fallback", False)
    codec_tuning.allow_software_fallback = manager.allow_software_fallback
    for camera in store.cameras():
        # Validate persisted config and discard legacy runtime fields.
        config = CameraConfig.model_validate(camera).model_dump()
        camera = {**camera, **config, "status": "disconnected", "last_error": None}
        await manager.add(camera)
    refresh_discovery()
    # Populate all rule identities before an inference task can emit its first
    # formal trigger, including while another camera is still starting.
    for camera_id in manager.cameras:
        tasks[camera_id] = asyncio.create_task(inference_loop(camera_id))
    await refresh_rtsp_credentials()
    await rtsp_output.start()
    await onvif_gateway.start_discovery()
    tasks["_retention"] = asyncio.create_task(retention_loop())


async def retention_loop():
    while True:
        try:
            settings = _reconcile_saved_base_url(store.get("settings"))
            await run_worker(
                store.purge,
                settings.get("retention_days", 7),
                int(settings.get("max_event_storage_mb", 2048)) * 1024 * 1024,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("事件保留清理失败")
        await asyncio.sleep(300)


async def shutdown():
    global store, inference_executor, rtsp_output
    # Stop media consumers before closing capture fds and BPU resources.
    if rtsp_output is not None:
        await rtsp_output.stop()
    await close_peers()
    for task in tasks.values():
        task.cancel()
    await asyncio.gather(*tasks.values(), return_exceptions=True)
    tasks.clear()
    if inference_executor is not None:
        executor, inference_executor = inference_executor, None
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
    await manager.close()
    await onvif_gateway.stop()
    manager.status_listener = None
    await close_peers()
    if store:
        store.close()
        store = None
    rtsp_output = None
    onvif_events.reset()


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": "0.1.0", "capture_bin": CAPTURE_BIN}


def set_session_cookie(response, token, request):
    response.set_cookie(
        "aix5_session",
        token,
        max_age=43200,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )


@app.post("/api/auth/login")
async def login(req: Login, response: Response, request: Request):
    async with auth_lock:
        if req.username != store.get("username", "admin"):
            raise HTTPException(401, "用户名或密码错误")
        try:
            await run_worker(
                PasswordHasher().verify, store.get("password_hash"), req.password
            )
        except (VerificationError, InvalidHashError):
            raise HTTPException(401, "用户名或密码错误")
        await run_worker(store.set_onvif_password, req.password)
        await refresh_rtsp_credentials()
        set_session_cookie(response, store.create_session(), request)
        return {"ok": True}


@app.post("/api/auth/password")
async def change_password(
    req: PasswordChange, response: Response, request: Request, _: bool = Depends(auth)
):
    async with auth_lock:
        try:
            await run_worker(
                PasswordHasher().verify,
                store.get("password_hash"),
                req.current_password,
            )
        except (VerificationError, InvalidHashError):
            raise HTTPException(400, "当前密码错误")
        password_hash = await run_worker(PasswordHasher().hash, req.new_password)
        store.change_password(password_hash, req.new_password)
        await refresh_rtsp_credentials()
        set_session_cookie(response, store.create_session(), request)
        return {"ok": True}


@app.get("/api/auth/account")
async def account(_: bool = Depends(auth)):
    return {"username": store.get("username"), "single_user": True}


@app.patch("/api/auth/account")
async def change_account(
    req: AccountChange,
    response: Response,
    request: Request,
    session: str | None = Cookie(default=None, alias="aix5_session"),
    _: bool = Depends(auth),
):
    async with auth_lock:
        # Recheck after taking the lock: another account update may have
        # revoked this request's session while password hashing was running.
        if not store.valid_session(session):
            raise HTTPException(401, "请重新登录")
        try:
            await run_worker(
                PasswordHasher().verify,
                store.get("password_hash"),
                req.current_password,
            )
        except (VerificationError, InvalidHashError):
            raise HTTPException(400, "当前密码错误")
        username = req.username if req.username is not None else store.get("username")
        password_hash = (
            await run_worker(PasswordHasher().hash, req.new_password)
            if req.new_password
            else None
        )
        store.change_account(username, password_hash, req.new_password or req.current_password)
        await refresh_rtsp_credentials()
        set_session_cookie(response, store.create_session(), request)
        return {"ok": True, "username": username, "single_user": True}


@app.post("/api/auth/logout")
async def logout(
    response: Response, session: str | None = Cookie(default=None, alias="aix5_session")
):
    if session:
        store.logout(session)
    response.delete_cookie("aix5_session", path="/")
    return {"ok": True}


@app.get("/api/system/status")
async def status(request: Request, _: bool = Depends(auth)):
    system = metrics.snapshot()
    system["webrtc_viewers"] = len(peer_connections)
    system["webrtc_codec"] = "H264 on demand"
    system["video_encoders"] = list(codec_tuning.encoder_states.values())
    system["rtsp_output"] = rtsp_output.snapshot() if rtsp_output else None
    return {
        "system": system,
        "cameras": [camera_view(c) for c in manager.cameras.values()],
        "storage": public_settings(request),
    }


@app.get("/api/audio/status")
async def audio_status(_: bool = Depends(auth)):
    fields = (
        "id",
        "audio_present",
        "audio_status",
        "audio_codec",
        "audio_level",
        "audio_dbfs",
    )
    return [
        {key: camera.get(key) for key in fields} for camera in manager.cameras.values()
    ]


@app.get("/api/cameras")
async def cameras(_: bool = Depends(auth)):
    return [camera_view(c) for c in manager.cameras.values()]


@app.put("/api/cameras/order")
async def reorder_cameras(req: CameraOrder, _: bool = Depends(auth)):
    async with camera_lock:
        try:
            store.reorder_cameras(req.camera_ids)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        # Keep camera objects, workers, queues and peer connections intact.
        manager.cameras = {cid: manager.cameras[cid] for cid in req.camera_ids}
        refresh_discovery()
        return [camera_view(c) for c in manager.cameras.values()]


@app.post("/api/cameras")
async def add_camera(req: CameraConfig, _: bool = Depends(auth)):
    async with camera_lock:
        if len(manager.cameras) >= 4:
            raise HTTPException(409, "最多支持 4 路摄像头")
        camera = {
            **req.model_dump(),
            "id": str(uuid.uuid4()),
            "source_type": "rtsp",
            "created_at": time.time(),
        }
        store.save_camera(camera)
        await manager.add(camera)
        tasks[camera["id"]] = asyncio.create_task(inference_loop(camera["id"]))
        refresh_discovery()
        return camera_view(camera)


@app.patch("/api/cameras/{cid}")
async def update_camera(cid: str, req: CameraPatch, _: bool = Depends(auth)):
    async with camera_lock:
        camera = manager.cameras.get(cid)
        if not camera:
            raise HTTPException(404, "摄像头不存在")
        try:
            config = CameraConfig.model_validate(
                {**camera, **req.model_dump(exclude_unset=True)}
            ).model_dump()
        except ValidationError as exc:
            raise HTTPException(422, exc.errors(include_context=False))
        restart = any(
            camera.get(k) != config[k] for k in ("rtsp_url", "transport", "enabled")
        )
        changed = any(
            camera.get(k) != config[k] for k in ("model_id", "enabled_labels")
        )
        camera.update(config)
        store.save_camera(camera)
        if restart:
            await manager.workers[cid].reconnect()
            await manager.reconfigure_audio(cid)
            await close_peers(cid)
        if changed:
            manager.clear_frames(cid)
            engine.reset_camera(cid)
            onvif_events.clear_camera(cid)
            camera_state(camera)
        refresh_discovery()
        return camera_view(camera)


@app.patch("/api/cameras/{cid}/labels")
async def update_camera_labels(cid: str, req: CameraLabels, _: bool = Depends(auth)):
    return await update_camera(
        cid, CameraPatch(enabled_labels=req.enabled_labels), True
    )


@app.post("/api/cameras/{cid}/reconnect")
async def reconnect(cid: str, _: bool = Depends(auth)):
    async with camera_lock:
        if cid not in manager.workers:
            raise HTTPException(404, "摄像头不存在")
        if not manager.cameras[cid].get("enabled", True):
            raise HTTPException(409, "摄像头已停用，请先启用")
        manager.cameras[cid].pop("encode_error", None)
        await manager.workers[cid].reconnect()
        await manager.reconfigure_audio(cid)
        await close_peers(cid)
        return {"ok": True}


@app.delete("/api/cameras/{cid}")
async def delete_camera(cid: str, _: bool = Depends(auth)):
    async with camera_lock:
        if cid not in manager.cameras:
            raise HTTPException(404, "摄像头不存在")
        task = tasks.pop(cid, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.remove(cid)
        await close_peers(cid)
        engine.reset_camera(cid)
        store.delete_camera(cid)
        onvif_events.remove_camera(cid)
        refresh_discovery()
        return {"ok": True}


@app.post("/api/onvif/discover")
async def onvif(request: Request, _: bool = Depends(auth)):
    _sync_request_public_base(request)
    devices = await discover()
    # The gateway also answers WS-Discovery for Home Assistant. Keep it out of
    # its own camera picker so users do not accidentally add AIX5 as a source.
    base_host = urlsplit(store.get("settings", {}).get("base_url", "")).hostname
    return [device for device in devices if not base_host or device.get("host") != base_host]


@app.post("/api/onvif/probe")
async def onvif_probe(req: ONVIFProbe, _: bool = Depends(auth)):
    try:
        return await probe(req.host, req.port, req.username, req.password)
    except Exception as exc:
        log.warning("ONVIF probe failed host=%s error=%s", req.host, exc)
        raise HTTPException(502, "ONVIF 连接或认证失败，请检查地址、端口和账号密码")


@app.get("/api/onvif/events")
async def onvif_event_status(_: bool = Depends(auth)):
    """Return current PullPoint state for diagnostics and integration QA."""

    return onvif_events.snapshot()


async def _onvif_base(request: Request):
    return _sync_request_public_base(request)


async def _onvif_basic_auth(request: Request, body: str = ""):
    """Verify Basic or WS-Security credentials with replay protection."""
    async with auth_lock:
        try:
            await run_worker(
                authenticate_onvif, store, request.headers.get("authorization", ""), body
            )
            await refresh_rtsp_credentials()
        except OnvifAuthError as exc:
            raise HTTPException(
                401, str(exc), headers={"WWW-Authenticate": 'Basic realm="AIX5 ONVIF"'}
            ) from None


@app.post("/onvif/{service}")
async def onvif_service(service: str, request: Request):
    if service not in {
        "device_service", "media_service", "events_service", "analytics_service"
    }:
        raise HTTPException(404, "ONVIF 服务不存在")
    body = (await request.body()).decode("utf-8", "replace")
    # ONVIF clients read UTC before calculating a WSSE digest with clock skew.
    if not (service == "device_service" and _action_name(body) == "GetSystemDateAndTime"):
        try:
            await _onvif_basic_auth(request, body)
        except HTTPException as exc:
            return Response(_fault(exc.detail, "NotAuthorized"), status_code=401,
                            media_type="application/soap+xml", headers=exc.headers)
    request_base = await _onvif_base(request)
    return Response(
        await onvif_gateway.handle(service, body, request_base),
        media_type="application/soap+xml",
    )


@app.post("/onvif/events/subscriptions/{token}")
async def onvif_subscription(token: str, request: Request):
    body = (await request.body()).decode("utf-8", "replace")
    try:
        await _onvif_basic_auth(request, body)
    except HTTPException as exc:
        return Response(_fault(exc.detail, "NotAuthorized"), status_code=401,
                        media_type="application/soap+xml", headers=exc.headers)
    await _onvif_base(request)
    envelope_body = re.search(
        r"<(?:[\w.-]+:)?Body\b[^>]*>(.*?)</(?:[\w.-]+:)?Body>",
        body,
        re.DOTALL,
    )
    action = re.search(
        r"<(?:[\w.-]+:)?([A-Za-z][\w.-]*)\b",
        envelope_body.group(1) if envelope_body else body,
    )
    action_name = action.group(1) if action else ""
    return Response(
        await onvif_gateway.pull(token, action_name, body),
        media_type="application/soap+xml",
    )


@app.get("/onvif/snapshots/{cid}.jpg")
async def onvif_snapshot(cid: str, request: Request):
    """Snapshot URI advertised by the ONVIF Media service."""

    await _onvif_basic_auth(request)
    if cid not in manager.cameras:
        raise HTTPException(404, "摄像头不存在")
    image = manager.frames.get(cid)
    if not image:
        raise HTTPException(404, "尚未收到视频帧")
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/models")
async def models(_: bool = Depends(auth)):
    return [
        {"id": k, **v, "available": bool(v["path"] and Path(v["path"]).exists())}
        for k, v in MODELS.items()
    ]


@app.get("/api/detection-labels")
async def detection_labels(_: bool = Depends(auth)):
    groups = {
        label: group
        for group, labels_in_group in COCO_GROUPS.items()
        for label in labels_in_group
    }
    return [
        {
            "id": label,
            "name": COCO_LABELS_ZH.get(label, label),
            "group": groups.get(label, "其他目标"),
        }
        for label in COCO_LABELS
    ]


@app.get("/api/cameras/{cid}/rules")
async def get_rules(cid: str, _: bool = Depends(auth)):
    return store.rules(cid)


@app.get("/api/rules")
async def get_all_rules(_: bool = Depends(auth)):
    cameras = {camera["id"]: camera for camera in store.cameras()}
    return [
        {**rule, "camera_name": cameras.get(rule["camera_id"], {}).get("name", rule["camera_id"])}
        for rule in store.rules(newest_first=True)
    ]


@app.get("/api/cameras/{cid}/rule-options")
async def get_rule_options(cid: str, _: bool = Depends(auth)):
    camera = manager.cameras.get(cid)
    if camera is None:
        raise HTTPException(404, "摄像头不存在")
    model_id = camera.get("model_id", "yolo11n")
    model = MODELS[model_id]
    threshold = model_confidence_threshold(model_id)
    enabled = set(camera.get("enabled_labels", DEFAULT_ENABLED_LABELS))
    labels = [
        {"id": label, "name": COCO_LABELS_ZH.get(label, label)}
        for label in model["labels"] if label in enabled
    ]
    return {
        "camera_id": cid,
        "model_id": model_id,
        "model_name": model["name"],
        "confidence_threshold": threshold,
        "default_confidence_threshold": threshold,
        "min_confidence_threshold": threshold,
        "max_confidence_threshold": 0.99,
        "enabled_labels": labels,
        "event_types": [
            {"id": "target_detected", "name": "目标检测", "requires_zone": False},
            {"id": "target_entered_zone", "name": "区域检测", "requires_zone": True},
        ],
    }


def validate_rule_threshold(camera, threshold, previous=None):
    floor = model_confidence_threshold(camera.get("model_id", "yolo11n"))
    if threshold < floor and (previous is None or threshold != previous):
        raise HTTPException(422, f"规则置信度不能低于当前算法的检测阈值 {floor:g}")


@app.post("/api/cameras/{cid}/rules")
async def add_rule(cid: str, req: RuleConfig, _: bool = Depends(auth)):
    if cid not in manager.cameras:
        raise HTTPException(404, "摄像头不存在")
    invalid = [x for x in req.target_labels if x not in set(COCO_LABELS)]
    if invalid:
        raise HTTPException(422, "不支持的目标类别：" + ", ".join(invalid))
    disabled = [
        x
        for x in req.target_labels
        if x
        not in set(manager.cameras[cid].get("enabled_labels", DEFAULT_ENABLED_LABELS))
    ]
    if disabled:
        raise HTTPException(422, "类别未在该摄像头启用：" + ", ".join(disabled))
    r = req.model_dump()
    if "confidence_threshold" not in req.model_fields_set:
        r["confidence_threshold"] = model_confidence_threshold(manager.cameras[cid].get("model_id", "yolo11n"))
    validate_rule_threshold(manager.cameras[cid], r["confidence_threshold"])
    r.update(id=str(uuid.uuid4()), camera_id=cid)
    store.save_rule(r)
    camera = manager.cameras[cid]
    onvif_events.register_rule(
        cid, r, camera_name=camera.get("name", cid),
        camera_enabled=camera.get("enabled", True),
    )
    return r


@app.patch("/api/rules/{rid}")
async def patch_rule(rid: str, req: RulePatch, _: bool = Depends(auth)):
    old = next((r for r in store.rules() if r["id"] == rid), None)
    if not old:
        raise HTTPException(404, "规则不存在")
    cam = manager.cameras.get(old["camera_id"])
    if not cam:
        raise HTTPException(404, "摄像头不存在")
    try:
        updated = RuleConfig.model_validate({**old, **req.model_dump(exclude_unset=True)})
    except ValidationError as exc:
        raise HTTPException(422, json.loads(exc.json())) from exc
    invalid = [x for x in updated.target_labels if x not in set(COCO_LABELS)]
    if invalid:
        raise HTTPException(422, "不支持的目标类别：" + ", ".join(invalid))
    disabled = [
        x
        for x in updated.target_labels
        if x not in set(cam.get("enabled_labels", DEFAULT_ENABLED_LABELS))
    ]
    if disabled and updated.target_labels != old.get("target_labels"):
        raise HTTPException(422, "类别未在该摄像头启用：" + ", ".join(disabled))
    r = updated.model_dump()
    validate_rule_threshold(cam, r["confidence_threshold"], old.get("confidence_threshold"))
    r.update(id=rid, camera_id=old["camera_id"])
    store.save_rule(r)
    # A display-name change keeps both the rule episode and its ONVIF identity.
    changed_conditions = (
        RuleConfig.model_validate(old).model_dump(exclude={"name"})
        != RuleConfig.model_validate(r).model_dump(exclude={"name"})
    )
    if changed_conditions:
        onvif_events.clear_rule(old["camera_id"], rid)
        engine.drop_rule(rid)
    onvif_events.register_rule(
        old["camera_id"], r, camera_name=cam.get("name", old["camera_id"]),
        camera_enabled=cam.get("enabled", True),
    )
    return r


@app.delete("/api/rules/{rid}")
async def remove_rule(rid: str, _: bool = Depends(auth)):
    old = next((r for r in store.rules() if r["id"] == rid), None)
    if not old:
        raise HTTPException(404, "规则不存在")
    store.delete_rule(rid)
    onvif_events.remove_rule(old["camera_id"], rid)
    engine.drop_rule(rid)
    return {"ok": True}


@app.get("/api/events")
async def events(
    request: Request,
    camera_id: str | None = None,
    event_type: str | None = None,
    target_type: str | None = None,
    limit: int = 100,
    _: bool = Depends(auth),
):
    rows = store.events(camera_id, max(1, min(limit, 500)), event_type, target_type)
    return _public_event_rows(rows, request)


@app.get("/api/events/export")
async def export_events(
    request: Request,
    format: str = "json",
    camera_id: str | None = None,
    event_type: str | None = None,
    target_type: str | None = None,
    _: bool = Depends(auth),
):
    rows = store.events(camera_id, 5000, event_type, target_type)
    rows = _public_event_rows(rows, request)
    if format.lower() == "csv":
        out = io.StringIO()
        fields = [
            "id",
            "timestamp",
            "camera_name",
            "event_type",
            "target_type",
            "confidence",
            "screenshot_url",
        ]
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})
        return Response(
            out.getvalue().encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="aix5-events.csv"'},
        )
    return Response(
        json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="aix5-events.json"'},
    )


@app.get("/api/events/{eid}/image")
async def event_image(
    eid: str,
    token: str | None = None,
    session: str | None = Cookie(default=None, alias="aix5_session"),
):
    if not ((token and token == store.get("ha_token")) or store.valid_session(session)):
        raise HTTPException(401, "请先登录")
    event = store.event(eid)
    if not event:
        raise HTTPException(404, "事件不存在")
    p = Path(event["image_path"])
    return (
        Response(p.read_bytes(), media_type="image/jpeg")
        if p.is_file()
        else (_ for _ in ()).throw(HTTPException(404, "截图不存在"))
    )


@app.get("/api/storage/status")
async def storage_status(_: bool = Depends(auth)):
    return await run_worker(store.storage_status)


@app.post("/api/storage/cleanup")
async def storage_cleanup(_: bool = Depends(auth)):
    return await run_worker(store.cleanup_storage)


def public_settings(request: Request | None = None):
    if request is not None:
        _sync_request_public_base(request)
    settings = {**DEFAULT_SETTINGS, **store.get("settings", {})}
    return settings


def _public_event_rows(rows, request: Request):
    """Rebuild screenshot origins from the current public address.

    Older event payloads may contain an origin captured before a DHCP change.
    Keep their stored payload untouched while returning a usable URL to the
    current client.
    """

    base = _sync_request_public_base(request)
    result = []
    for row in rows:
        item = dict(row)
        if item.get("id"):
            item["screenshot_url"] = (
                f"{base}/api/events/{item['id']}/image?token={store.get('ha_token')}"
            )
        result.append(item)
    return result


@app.get("/api/settings")
async def settings(request: Request, _: bool = Depends(auth)):
    return public_settings(request)


@app.patch("/api/settings")
async def patch_settings(req: SettingsPatch, _: bool = Depends(auth)):
    async with settings_lock:
        old = store.get("settings")
        updates = req.model_dump(exclude_unset=True)
        try:
            s = SettingsConfig.model_validate({**old, **updates}).model_dump()
        except ValidationError as exc:
            raise HTTPException(422, exc.errors(include_context=False))
        s = _reconcile_saved_base_url(s)
        store.set("settings", s)
        manager.preview_resolution = s["preview_resolution"]
        manager.allow_software_fallback = s["allow_software_fallback"]
        codec_tuning.allow_software_fallback = s["allow_software_fallback"]
        configure_onvif_gateway(s["base_url"])
        if old.get("allow_software_fallback", False) != s["allow_software_fallback"]:
            for camera in manager.cameras.values():
                camera.pop("encode_error", None)
            for cid in list(manager.workers):
                if manager.cameras[cid].get("enabled", True):
                    await manager.workers[cid].reconnect()
            await close_peers()
        if old.get("preview_resolution", "960x540") != s["preview_resolution"]:
            manager.frames.clear()
            manager.preview_next.clear()
        return {"ok": True}


@app.get("/api/settings/export")
async def export_settings(_: bool = Depends(auth)):
    payload = {
        "cameras": [
            {
                **{
                    k: v
                    for k, v in c.items()
                    if k not in ("last_frame", "status", "last_error")
                },
                "rtsp_url": redact_url(c.get("rtsp_url", "")),
            }
            for c in store.cameras()
        ],
        "rules": store.rules(),
        "settings": store.get("settings"),
    }
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="aix5-config.json"'},
    )


@app.get("/api/cameras/{cid}/preview.jpg")
async def preview(cid: str, _: bool = Depends(auth)):
    if cid not in manager.frames:
        raise HTTPException(404, "尚未收到视频帧")
    return Response(
        manager.frames[cid],
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/cameras/{cid}/webrtc/offer")
async def webrtc_offer(cid: str, request: Request, _: bool = Depends(auth)):
    if cid not in manager.cameras:
        raise HTTPException(404, "摄像头不存在")
    if manager.cameras[cid].get("status") != "running":
        raise HTTPException(409, "摄像头尚未在线")
    payload = await request.json()
    if not payload.get("sdp") or not payload.get("type"):
        raise HTTPException(400, "WebRTC offer 无效")
    mode = payload.get("mode", "grid")
    if mode not in ("grid", "fullscreen"):
        mode = "grid"
    try:
        return await create_offer_answer(cid, payload["sdp"], payload["type"], mode)
    except PeerLimitError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        log.exception("WebRTC offer failed camera=%s", cid)
        raise HTTPException(502, "WebRTC 建立失败：" + str(exc))


async def mjpeg_gen(cid):
    last = None
    while cid in manager.cameras:
        f = manager.frames.get(cid)
        if f and f is not last:
            last = f
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                + str(len(f)).encode()
                + b"\r\n\r\n"
                + f
                + b"\r\n"
            )
        await asyncio.sleep(0.04)


@app.get("/api/cameras/{cid}/mjpeg")
async def mjpeg(cid: str, _: bool = Depends(auth)):
    if cid not in manager.cameras:
        raise HTTPException(404, "摄像头不存在")
    return StreamingResponse(
        mjpeg_gen(cid), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.websocket("/ws/preview")
async def ws_preview(ws: WebSocket):
    if not store.valid_session(ws.cookies.get("aix5_session")):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        while True:
            await ws.send_json(
                {"cameras": [camera_view(c) for c in manager.cameras.values()]}
            )
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(WEB_INDEX)


def main():
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
