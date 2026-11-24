# Telegram File-to-Link

Bot de Telegram que copia videos y documentos a un canal privado y crea enlaces
publicos para streaming con soporte para HTTP Range y descarga directa.

## Despliegue en Render

1. Crea un servicio **Web Service** desde este repositorio y selecciona **Docker**.
2. Agrega las variables de Telegram en **Environment**. Guarda `API_HASH` y
   `BOT_TOKEN` como secretos.
3. Agrega el bot como administrador del canal privado configurado en `BIN_CHANNEL`,
   con permiso para publicar mensajes.
4. Render proporciona `PORT` y `RENDER_EXTERNAL_URL` automaticamente. El contenedor
   escucha en el puerto de `PORT`; si no se define, usa `7860`.

| Variable | Configuracion en Render | Descripcion |
| --- | --- | --- |
| `API_ID` | Obligatoria | API ID de Telegram |
| `API_HASH` | Obligatoria, secreto | API hash de Telegram |
| `BOT_TOKEN` | Obligatoria, secreto | Token del bot |
| `BIN_CHANNEL` | Obligatoria | ID numerico del canal privado, normalmente empieza por `-100` |
| `PORT` | Automatica | Puerto HTTP que Render asigna al servicio |
| `RENDER_EXTERNAL_URL` | Automatica | URL publica que Render proporciona para el Web Service |
| `URL` | Opcional | URL publica manual; si se define, tiene prioridad sobre `RENDER_EXTERNAL_URL` |
| `OWNER_ID` | Opcional | Si se define, solo este ID de usuario puede generar enlaces |
| `WORKER_MODE` | Opcional | Con valor `true`, desactiva el manejador de mensajes y deja el proceso atendiendo streaming |
| `ALLOWED_GROUP_ID` | Opcional | ID del unico grupo o supergrupo autorizado para generar enlaces |
| `SESSION_STRING` | Opcional, secreto | Cadena de sesion de Telethon (StringSession); si se configura, no se crea un archivo de sesion |

El servidor tambien acepta `FQDN` o `SPACE_HOST` como alternativas a la URL publica.
El endpoint `/` responde `{"status":"running"}` para las comprobaciones de salud.
Para nodos adicionales de streaming, configura `WORKER_MODE=true`; el cliente de
Telegram y las rutas HTTP seguiran activos, pero los archivos enviados no generaran
enlaces desde ese nodo. De forma predeterminada (`false`), el proceso conserva el
comportamiento completo del bot.

## Uso

En un chat privado, envia o reenvia un video o documento al bot. Una vez copiado
al canal de almacenamiento, el bot responde con:

- `https://.../stream/{message_id}` para reproduccion en linea.
- `https://.../dl/{message_id}` para descarga como adjunto.

El endpoint de streaming entiende rangos de bytes individuales, incluidos rangos
abiertos y de sufijo. Los datos se leen desde Telegram por bloques y no se guardan
en el disco del servicio. Cualquiera que tenga uno de los enlaces puede acceder al
archivo; los enlaces no requieren autenticacion. No compartas archivos sin permiso
y elimina del canal cualquier contenido que ya no deba estar disponible.

La respuesta intenta usar botones nativos `Button.copy` cuando la version de
Telethon los ofrece; si no, los enlaces permanecen en etiquetas HTML `<code>` para
copiarlos desde el mensaje. Telethon 1.46.0, fijado en `requirements.txt`, aun no
incluye `Button.copy`, por lo que usa el mensaje de respaldo hasta que se actualice
a una version que lo implemente.

El almacenamiento depende de que el mensaje permanezca en el canal privado:
si se elimina del canal, el enlace deja de funcionar.

Telethon mantiene la reconexion automatica ante cortes transitorios de MTProto;
los errores de configuracion o de permisos se registran en los logs.
`SESSION_STRING` debe ser generado por Telethon: no es compatible con cadenas
previamente exportadas por Pyrogram o Pyrofork.
