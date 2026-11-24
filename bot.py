import asyncio
import html
import logging
import math
import mimetypes
import os
import re
import signal
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from aiohttp import web
from telethon import Button, TelegramClient, events, types
from telethon.errors import RpcCallFailError
from telethon.sessions import StringSession

CHUNK_SIZE = 1024 * 1024
RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)$")
LOG = logging.getLogger("file_to_link")


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    bin_channel: int
    port: int
    public_url: str
    owner_id: int | None
    allowed_group_id: int | None

    @classmethod
    def from_env(cls) -> "Settings":
        required = ("API_ID", "API_HASH", "BOT_TOKEN", "BIN_CHANNEL")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise RuntimeError(f"Faltan variables de entorno: {', '.join(missing)}")

        raw_url = (
            os.environ.get("URL")
            or os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("FQDN")
            or os.environ.get("SPACE_HOST")
        )
        if not raw_url:
            raise RuntimeError(
                "Define URL o una URL publica mediante RENDER_EXTERNAL_URL, FQDN o SPACE_HOST"
            )
        public_url = normalize_public_url(raw_url)

        owner_value = os.environ.get("OWNER_ID")
        allowed_group_value = os.environ.get("ALLOWED_GROUP_ID")
        try:
            return cls(
                api_id=int(os.environ["API_ID"]),
                api_hash=os.environ["API_HASH"],
                bot_token=os.environ["BOT_TOKEN"],
                bin_channel=int(os.environ["BIN_CHANNEL"]),
                port=int(os.environ.get("PORT", 7860)),
                public_url=public_url,
                owner_id=int(owner_value) if owner_value else None,
                allowed_group_id=(
                    int(allowed_group_value) if allowed_group_value else None
                ),
            )
        except ValueError as exc:
            raise RuntimeError(
                "API_ID, BIN_CHANNEL, PORT, OWNER_ID y ALLOWED_GROUP_ID deben ser numeros"
            ) from exc


def normalize_public_url(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = f"https://{value}"
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise RuntimeError("URL debe ser un host o una URL HTTP/HTTPS valida")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def get_file(message, fallback_name: str = "Archivo sin nombre"):
    document = getattr(message, "document", None)
    if document is None:
        return None

    filename = next(
        (
            attribute.file_name
            for attribute in document.attributes
            if isinstance(attribute, types.DocumentAttributeFilename)
        ),
        fallback_name,
    )
    mime_type = document.mime_type or mimetypes.guess_type(filename)[0]
    return document, filename, mime_type or "application/octet-stream"


def content_disposition(kind: str, filename: str) -> str:
    safe_name = filename.replace("\r", "").replace("\n", "").replace('"', "'")
    ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "file"
    encoded_name = quote(safe_name, safe="")
    return f'{kind}; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'


def parse_byte_range(value: str, size: int) -> tuple[int, int] | None:
    match = RANGE_PATTERN.fullmatch(value.strip())
    if not match or size <= 0:
        return None

    first, last = match.groups()
    if not first and not last:
        return None
    if not first:
        suffix_length = int(last)
        if suffix_length <= 0:
            return None
        return max(0, size - suffix_length), size - 1

    start = int(first)
    if start >= size:
        return None
    end = int(last) if last else size - 1
    if end < start:
        return None
    return start, min(end, size - 1)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "running"})


async def serve_file(request: web.Request) -> web.StreamResponse:
    client: TelegramClient = request.app["telegram_client"]
    channel = request.app["bin_channel"]
    try:
        message_id = int(request.match_info["message_id"])
    except ValueError:
        raise web.HTTPNotFound()

    try:
        message = await client.get_messages(channel, ids=message_id)
    except Exception as exc:
        LOG.exception("No se pudo obtener el mensaje %s del canal", message_id)
        raise web.HTTPBadGateway(
            text="No se pudo recuperar el archivo desde Telegram"
        ) from exc
    if not message or not message.id:
        raise web.HTTPNotFound(text="Archivo no encontrado")

    file_info = get_file(message, f"file_{message_id}")
    if file_info is None:
        raise web.HTTPNotFound(text="El mensaje no contiene un video o documento")
    document, filename, mime_type = file_info
    size = document.size

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime_type,
        "Content-Disposition": content_disposition(
            "attachment" if request.path.startswith("/dl/") else "inline",
            filename,
        ),
        "X-Content-Type-Options": "nosniff",
    }

    range_header = request.headers.get("Range")
    start, end = 0, size - 1
    status = 200
    if range_header:
        byte_range = parse_byte_range(range_header, size)
        if byte_range is None:
            raise web.HTTPRequestRangeNotSatisfiable(
                headers={"Content-Range": f"bytes */{size}"}
            )
        start, end = byte_range
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    content_length = max(0, end - start + 1)
    headers["Content-Length"] = str(content_length)
    response = web.StreamResponse(status=status, headers=headers)
    if request.method == "HEAD":
        return response

    await response.prepare(request)
    if content_length == 0:
        await response.write_eof()
        return response

    sent = 0
    try:
        async for chunk in client.iter_download(
            message.media,
            offset=start,
            limit=math.ceil(content_length / CHUNK_SIZE),
            chunk_size=CHUNK_SIZE,
            file_size=size,
        ):
            remaining = content_length - sent
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            if chunk:
                await response.write(bytes(chunk))
                sent += len(chunk)
            if sent >= content_length:
                break
    except (ConnectionResetError, BrokenPipeError):
        LOG.debug("El cliente cerro la conexion durante la descarga del mensaje %s", message_id)
        return response
    except Exception:
        LOG.exception("Error transmitiendo el mensaje %s desde Telegram", message_id)
        return response

    if sent != content_length:
        LOG.error(
            "Transmision incompleta para el mensaje %s: %s de %s bytes",
            message_id,
            sent,
            content_length,
        )
        return response
    await response.write_eof()
    return response


def is_authorized(event, settings: Settings) -> bool:
    if event.is_private:
        return (
            event.sender_id is not None
            and (settings.owner_id is None or event.sender_id == settings.owner_id)
        )

    if event.is_group:
        return (
            settings.allowed_group_id is not None
            and event.chat_id == settings.allowed_group_id
        )

    return False


async def handle_upload(client: TelegramClient, event, settings: Settings) -> None:
    if not is_authorized(event, settings):
        LOG.info(
            "Ignoring media message %s from unauthorized chat %s",
            event.id,
            event.chat_id,
        )
        return

    file_info = get_file(event.message)
    if file_info is None:
        return
    _, filename, _ = file_info

    try:
        stored_message = await client.forward_messages(
            settings.bin_channel,
            event.message,
        )
    except Exception:
        LOG.exception("No se pudo copiar el mensaje %s al canal BIN_CHANNEL", event.id)
        await client.send_message(
            event.chat_id,
            "No se pudo guardar el archivo. Revisa el canal y sus permisos.",
            reply_to=event.id,
        )
        return

    if isinstance(stored_message, (list, tuple)):
        stored_message = stored_message[0]

    stream_url = f"{settings.public_url}/stream/{stored_message.id}"
    download_url = f"{settings.public_url}/dl/{stored_message.id}"
    response_text = (
        f"🎬 <b>{html.escape(filename)}</b>\n\n"
        f"▶️ <b>Streaming:</b> <code>{html.escape(stream_url)}</code>\n"
        f"⬇️ <b>Descarga:</b> <code>{html.escape(download_url)}</code>"
    )

    try:
        buttons = [[
            Button.copy("📋 Copiar Streaming", stream_url),
            Button.copy("📋 Copiar Descarga", download_url),
        ]]
        await client.send_message(
            event.chat_id,
            response_text,
            parse_mode="html",
            buttons=buttons,
            reply_to=event.id,
        )
    except Exception as exc:
        LOG.warning(
            "No se pudieron enviar botones de copia (%s); enviando enlaces sin botones",
            exc,
        )
        await client.send_message(
            event.chat_id,
            response_text,
            parse_mode="html",
            reply_to=event.id,
        )


async def resolve_bin_channel(client: TelegramClient, channel_id: int):
    try:
        return await client.get_input_entity(int(channel_id))
    except ValueError as cache_error:
        resolved_id, peer_type = utils.resolve_id(int(channel_id))
        if peer_type is not types.PeerChannel:
            raise RuntimeError(
                f"BIN_CHANNEL={channel_id} no es un ID de canal valido"
            ) from cache_error

        try:
            peer = types.PeerChannel(resolved_id)
            return await client.get_input_entity(peer)
        except ValueError:
            try:
                return await client.get_input_entity(
                    await client.get_entity(int(channel_id))
                )
            except Exception as exc:
                raise RuntimeError(
                    "No se pudo resolver BIN_CHANNEL sin una entidad en la cache. "
                    "Confirma que el bot tiene acceso al canal y que su entidad "
                    "ya esta disponible en la sesion de Telethon."
                ) from exc


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    session_string = os.environ.get("SESSION_STRING", "").strip()
    session = StringSession(session_string) if session_string else "file_to_link_bot"
    client = TelegramClient(session, settings.api_id, settings.api_hash)
    worker_mode = os.environ.get("WORKER_MODE", "false").lower() == "true"

    if worker_mode:
        LOG.info("WORKER_MODE activo: manejadores de mensajes deshabilitados")
    else:

        @client.on(events.NewMessage(incoming=True))
        async def on_upload(event) -> None:
            await handle_upload(client, event, settings)

    web_app = web.Application()
    web_app["telegram_client"] = client
    web_app["bin_channel"] = settings.bin_channel
    web_app.router.add_get("/", health)
    web_app.router.add_get("/stream/{message_id}", serve_file, name="stream")
    web_app.router.add_get("/dl/{message_id}", serve_file, name="download")

    runner = web.AppRunner(web_app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=settings.port)
        await site.start()
        LOG.info("Servidor HTTP activo en el puerto %s", settings.port)

        for attempt in range(1, 11):
            try:
                await client.start(bot_token=settings.bot_token)
                LOG.info("Cliente de Telegram iniciado en el intento %s", attempt)
                break
            except RpcCallFailError as exc:
                if attempt == 10:
                    LOG.exception(
                        "No se pudo iniciar Telegram despues de 10 intentos"
                    )
                    raise
                LOG.warning(
                    "RPC_CALL_FAIL al iniciar Telegram (intento %s/10): %s",
                    attempt,
                    exc,
                )
                await asyncio.sleep(10)

        web_app["bin_channel"] = await resolve_bin_channel(
            client,
            settings.bin_channel,
        )

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for stop_signal in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(stop_signal, stop_event.set)
        await stop_event.wait()
    finally:
        await runner.cleanup()
        if client.is_connected():
            await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
