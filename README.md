# Music Terminal

Cliente de **YouTube Music para la terminal de Linux**, sin dependencias:
solo Python 3 + librería estándar, con **mpv** como motor de audio.
La interfaz calca la de [AniLinux Terminal](https://github.com/PedroSG08/anilinux):
rail lateral de secciones, listas con flechas, diálogos centrados y barra de
estado, en español.
Código y descarga: [github.com/PedroSG08/music-terminal](https://github.com/PedroSG08/music-terminal).

```
╭───────────────────────────────────────────────────────────╮╭─♪  Reproductor───╮
 MUSIC TERMINAL          Biblioteca local · 26 canciones     │ ░░░░░░░░░░░░░░░░ │
 SECCIONES   │  Sonando: Bohemian Rhapsody — Queen           │ ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ │
 ▌ Inicio    │  son la TUI completa                          │ Bohemian Rhapsody│
   Explorar  │                                               │ Queen            │
   Buscar    │  el panel de la derecha aparece al reproducir │ ━━━─── 1:04/5:54 │
   Biblioteca│  (¼ del ancho, toda la altura)                │ [espacio] Pausa  │
             │                                               │ [   n  ] Siguiente
 ╰───────────────────────────────────────────────────────────╯│ [   y  ] Ver letra
                                                              ╰──────────────────╯
```

## Instalación

No hay dependencias de pip ni paquetes que compilar: solo hace falta
**Python 3.10+** y **mpv** (la lista completa con los opcionales está en
«Requisitos», más abajo).  Empieza por lo imprescindible:

```bash
sudo apt install mpv python3-pil wl-clipboard libsecret-tools
```

(en Fedora: `sudo dnf install mpv python3-pillow wl-clipboard libsecret`;
en Arch: `sudo pacman -S mpv python-pillow wl-clipboard libsecret` —
`python3-pil` solo mejora las portadas ANSI; sin él hay un panel
decorativo).

Luego clona el repo y deja el lanzador al alcance de la mano:

```bash
git clone https://github.com/PedroSG08/music-terminal.git
cd music-terminal
mkdir -p ~/.local/bin
ln -s "$(pwd)/music-terminal" ~/.local/bin/music-terminal
```

Si `~/.local/bin` no está en tu `PATH`, añádelo con:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

Arranca con `music-terminal` (o `./music-terminal` desde el clon) y
comprueba que todo respondió:

```bash
music-terminal --version
```

Dentro, lo primero suele ser Ajustes ▸ **Cuenta de Google** para
iniciar sesión con tu YouTube Music (ver «Sesión de Google»).

**Actualizar** tampoco tiene paso de reinstalación: el symlink apunta
al clon, así que `git pull` dentro de la carpeta y ya.

## Uso

```bash
music-terminal          # (symlink en ~/.local/bin) o ./music-terminal
music-terminal --region ES --lang es
music-terminal --version
```

Con **ventanas ≥ 96 columnas** y una canción cargada, la parte derecha de la
TUI es el **panel del reproductor** (⅓ de la pantalla): portada ANSI de alta
calidad — descarga la miniatura en la **mayor resolución disponible**
(`maxresdefault` 1280×720 → `hq720` → `hqdefault` → `mqdefault`), recorta
las bandas negras del letterbox, centra al aspecto **físico** de la región
y se renderiza con **blitter mixto**: cada celda elige entre **cuadrantes**
(2×2 píxeles: ▘▝▌▐▀▄▚▞▙▛▜▟█) u **octavos verticales** (▁▂▃▄▅▆▇ —
frontera fg/bg cada 2 px) el que comete menos error — los degradados
quedan con detalle vertical de 2 px en lugar de los 8 px de los medios
bloques clásicos; el resto del error se difunde **Floyd–Steinberg**
por píxel y la fuente recibe un toque previo de **nitidez**
(UnsharpMask).  Con terminales de **color verdadero**
(`COLORTERM=truecolor`) sale 24-bit sin cuantizar.  El arte queda
cacheado en disco (`covers/*.ansi`) — segunda vez, 0 ms.

**En kitty la portada es una imagen real.**  Antes de fiarse del
entorno, al arrancar manda una **sonda** (consulta `a=q` + `ESC[c`):
solo si la terminal responde a la consulta gráfica se usa el canal;
lo demás cae al arte ANSI de siempre.  El mismo recorte se guarda como
PNG **a resolución física exacta**: consulta con `TIOCGWINSZ` (respaldo
`CSI 16t`) los píxeles reales de UNA celda y genera el PNG de
`ancho_celdas×celda_w × alto_celdas×celda_h` — kitty no estira nada y
la portada sale 1:1 nítida.  En clientes
locales la transmisión va **por archivo** (`t=f`): cada repintado cuesta
~120 bytes (solo la ruta), no los ~300 KB del PNG; por SSH envía los
bytes en trozos de 4 KB (`m=1/0`).  Todo silencioso (`q=2`) y con el
cursor protegido (`C=1`); el origen de la imagen es **la celda del
cursor** al procesar el último escape, así que la app lo fija a la
esquina superior izquierda del área antes de cada colocación.  La
imagen se borra al cambiar de canción,
página o al salir (que `ESC[2J` también las borra, los repintados
recolocan ella solos).  En cualquier otra terminal sigue el render ANSI
mixto de arriba.  Título,
artista, barra de progreso y botones. Cambia de página con `o`:
**portada → cola → letra → portada**; el resto del programa sigue
navegable a la izquierda.

### Reproducción rápida
- **Escalera de clientes** en `player`: IOS anónimo (la ruta que YouTube
  admite hoy sin PoToken) → VISIONOS 0.1 → ANDROID_VR, con sonda de 40 KB
  (las URLs que el CDN contesta 403 se descartan al vuelo) y techo de
  5 s antes de pasar al fallback.
- **Sugerencias asíncronas** en el buscador: el eco del teclado **nunca
  espera a la red** (1.4.3 y anteriores bloqueaban cada letra con la
  ronda-trip de sugerencias).  Hay caché persistente (volver a abrir
  una consulta es instantáneo), una sola consulta por palabra y las
  filas de sugerencias se pintan navegables con ↑↓.
- **Cola persistente de sesión**: la última cola (ítems, posición, modo
  repetir, aleatorio y contexto) sobrevive al reinicio aunque el
  proceso muera sin cierre limpio — vigilante que la guarda cada ~2 s
  cuando cambia y handlers de SIGTERM/SIGHUP que la guardan antes de
  morir (cerrar la ventana de kitty, logout, kill).  Al arrancar la TUI
  reabre exactamente donde quedó y `espacio` reanuda desde ahí.
- **Cola aleatoria de una playlist**: en cualquier lista (playlist
  local, de cuenta, álbum o resultados) la tecla `s` baraja TODA la
  lista — las playlists con paginación se leen enteras vía loader
  (hasta 800) — la sustituye por la cola mezclada y arranca la primera.
  La última cola barajada persiste con el resto del guardado de sesión.
- **Motor de pista robusto** (adiós a los saltos del «hoy sí, hoy no»):
  mpv dice `eof` al terminar una canción por sí sola y **eso avanza la
  cola de verdad**; los errores a mitad (CDN corta la URL, 403…) ya NO
  saltan a la siguiente sin más — hay **un intento de rescate** que
  re-extrae el stream y retoma la pista en el último segundo conocido
  («reconectando la pista…»); si vuelve a romper, entonces sí pasa.  El
  IPC con mpv manda `request_id` y descarta los eventos difundidos, con
  lo que duración/pausa/posición nunca salen mascaradas (el indicador
  ya no enseña `?:??` de fantasía: cuando mpv conoce la duración de la
  pista se rellena también en la fila de la cola).
- **Portadas nítidas** (kitty): la fuente se exige al tamaño del panel —
  si lo que hay en caché es pequeño (miniaturas de 120 px que YTM trae
  en algunos renderers) se pide la escalera completa (lh3 reescrita a
  w1200, maxresdefault, hq720…) en vez de difuminar.  El PNG guarda por
  fuente, así que los viejos generados desde miniaturas pobres se
  regeneran solos; y al agrandar una fuente mediocrecillas lleva un
  pas de nitidez (UnsharpMask).
- **yt-dlp** (red de seguridad ya instalada) solo se invoca cuando la
  ruta nativa falla de verdad — y **una sola vez** por intento, ya no
  duplicada al comienzo.
- **Caché de streams en disco** (`streams.json`, 30 min, máx 120): replays
  del historial y de repeticiones arrancan **instantáneos**.
- **Prefetch**: al arrancar una canción, en segundo plano se resuelven
  las **2 siguientes de la cola** — `n` no espera.
- Las pantallas de **cuenta** (Me gusta, Historial, playlists de la
  cuenta) cargan con **spinner no bloqueante**: la TUI sigue viva
  mientras se pagina.

| Sección    | Contenido                                                                 |
|------------|---------------------------------------------------------------------------|
| **Inicio** | Favoritos, historial, continuar la última cola, radio aleatoria y estantes en vivo («Selecciones rápidas», «The sound of…», playlists destacadas…) |
| **Explorar**| Moods & géneros, listas destacadas, novedades y top (charts) de la región |
| **Buscar** | Búsqueda de YouTube Music con pestañas: canciones, vídeos, álbumes, artistas y listas; sugerencias en vivo mientras escribes |
| **Biblioteca** | Cuenta Google (Me gusta, playlists de tu cuenta, historial de YouTube) + favoritos, historial y playlists locales |
| **Cola**   | Cola de reproducción: reordenar, saltar, pausar, volumen, mezcla y repetición |
| **Ajustes**| Región, idioma, calidad de audio, repetición, extractor, helper yt-dlp, sesión de YouTube y **cuenta de Google** |

Teclas globales (mientras suena algo): `Espacio` pausa/seguir/iniciar ·
`n` siguiente · `v` anterior · `o` página del panel · `y` letra ·
`+/-` elegir página cola/letra · `Tab` saltar a la pista elegida del panel.
En cualquier pantalla: `↑↓` mover · `Enter/→` abrir · `Esc/←` atrás ·
`q` salir.  En listas: `Enter` reproducir · `a` encolar · `f` favorito local ·
`K` Me gusta (cuenta Google) · `r` radio · `l` letra · `c` copiar URL al
portapapeles · `←→` pestañas.
Los avisos (descargas, favoritos, errores…) salen en una **ventana
flotante arriba a la derecha** con borde según su tipo (verde aviso,
naranja problemas…) y caducan solos: la barra de abajo con las teclas
queda siempre legible.

## Sesión de Google (sincroniza con tu YouTube Music)

Ajustes ▸ **Cuenta de Google: sin sesión (copia de tu navegador)** y luego
Enter.  **Automático**: music-terminal abre `music.youtube.com` en tu
navegador, comprueba que estás con tu sesión de Google y, al volver y pulsar
Enter, **copia la cookie sola** leyendo el perfil de tu navegador:

- **Firefox y derivados** (Zen, LibreWolf, Waterfox, Floorp): lectura
  directa de `cookies.sqlite` (sin cifrar).
- **Navegadores Chromium** (Chrome, Brave, Edge, Vivaldi…): descifra
  `encrypted_value` (v10/v11: PBKDF2 + AES-128 implementados en puro
  Python; la contraseña del llavero se pide vía `secret-tool`).

El escaneo solo se ejecuta cuando tú lo pides (al iniciar sesión).  Si
algo falla (p. ej. llavero protegido), hay camino **manual** con la tecla
`m`: `F12` → Red → copia el encabezado `cookie` de una petición `youtubei`
y pégalo (o guárdalo en `~/.config/music-terminal/cookie.txt`).

La cookie se guarda en `~/.config/music-terminal/auth.json` (permisos 600)
y solo se envían las de dominio youtube.com que un navegador mandaría a
music.youtube.com (más el `VISITOR_INFO1_LIVE` como visitorData, que es lo
que liga la petición a tu sesión).
Con la sesión activa: se ven tus **Me gusta** (playlist LM, completa con
paginación — no se queda en las 100 primeras), tus **playlists
de la cuenta** (`FEmusic_liked_playlists`, la misma ruta de Metrolist, sin
límite de canciones al abrirlas), el
**historial de YouTube Music** y el **nombre de tu cuenta**
(`account/accounts_list`); `K` marca/quita Me gusta directamente en tu
cuenta; la reproducción envía un ping de seguimiento para
que tu historial de YouTube se sincronice. «Cerrar sesión» borra todo.

## Cómo consigue el audio

1. **API InnerTube propia** (replicada del código decompilado de Metrolist /
   innertubex, Java → Python stdlib):
   - *Navegación*: cliente **WEB_REMIX** (clientId 67,
     v1.20260707.12.00) contra `music.youtube.com`, con `visitorData`
     obtenido de la portada web y persistido en
     `~/.config/music-terminal/session.json`.
2. **Streams de audio**: endpoint `player` con escalera de clientes
   (IOS anónimo → VISIONOS 0.1 → ANDROID_VR). Cada URL candidata se
   sondea con **rango abierto** (`bytes=0-`): solo gana la primera que
   entrega el rango abierto (IOS lo hace en el grueso del catálogo; en
   fuentes que lo rechazan con 403 la escalera cae a **VISIONOS 0.1**
   (Apple Vision Pro, clientId 101), que sirve URLs **opus/m4a de rango
   abierto** que mpv reproduce directamente (verificado: GET completo =
   pista entera de 4,7 MB en una petición).
   - el cliente ANDROID_VR (también de innertubex) solo sirve los primeros
     ~1 MiB (403 después de esa frontera) y quedó documentado como
     «benchmark» en el propio catálogo de la app;
   - plan B: un **proxy HTTP local** (`streamproxy.py`) que trocea la URL
     en ventanas de 128 KiB por si una fuente volviera a ser de rango
     acotado → mpv escucha `http://127.0.0.1:<puerto>/audio`;
   - plan C: **yt-dlp** como helper opcional (se descarga desde Ajustes a
     `~/.local/share/music-terminal/bin/yt-dlp`).

## Copias sin internet (desde 1.4.10)

Guárdate canciones y playlists enteras en local y olvídate de la red una
vez están en la carpeta:

- En cualquier lista: **`d`** baja la canción resaltada y **`D`** la lista
  entera (si hay paginación, la drena hasta 800). Las filas guardadas
  llevan la marca **`⤓`**. En una **playlist de YouTube Music** (1.4.11),
  `D` además la espeja en **Playlists locales** (Biblioteca): mismo
  nombre, mismo orden; si ya existía, actualiza su contenido.
- En el menú **«Playlists de tu cuenta»** (Biblioteca ▸ Cuenta Google),
  **`d`** guarda la playlist resaltada en local: baja todas sus canciones
  y la espeja en **Playlists locales** con el mismo nombre y orden (crea
  la playlist o actualiza la que ya había). Abierta desde ese menú, `D`
  dentro de la playlist también la espeja.
- En **Ajustes**, «Guardar en local lo reproducido»: con la opción **ON**
  todo lo que suene va quedando copiado en segundo plano (truco: `D` en
  una playlist y en un rato la tienes completa en local).
- Ajustes → «Copias sin internet» (Enter) abre el **gestor de copias**:
  `Enter` libera la copia resaltada, `A` las vacía todas y `o` abre la
  carpeta en el explorador de ficheros.
- La reproducción es **primero local**: si hay copia, ni el extractor ni
  la red se tocan (con la app abierta sin conexión la cola y la
  biblioteca siguen sonando). En la barra se ve como `· copia`; las
  descargas guardan el mismo flujo que reproduce mpv en
  `~/.local/share/music-terminal/offline/<videoid>.<webm|m4a…>`.
- La descarga intenta el pedido completo; si el CDN lo rechaza con 403
  (fuente de rango acotado) rebaja a **ventanas de 128 KiB** y renueva la
  URL cuando el cupo se agota. Un bache pasajero de red no tumba la
  copia: el worker reintenta una vez antes de avisar.

## Datos

| Ruta                                        | Contenido                       |
|---------------------------------------------|---------------------------------|
| `~/.config/music-terminal/session.json`     | visitorData de la sesión        |
| `~/.config/music-terminal/settings.json`    | preferencias (volumen, calid…)  |
| `~/.config/music-terminal/auth.json`        | cookie de tu cuenta Google (600)|
| `~/.config/music-terminal/cookie.txt`       | alternativa para pegar la cookie|
| `~/.local/share/music-terminal/library.db`  | canciones, favoritos, historial, playlists y estado de la cola |
| `~/.local/share/music-terminal/covers/`     | miniaturas de portadas en caché |
| `~/.local/share/music-terminal/offline/`    | copias de audio (sin internet)  |

## Requisitos

- Python 3.10+ (solo stdlib)
- `mpv` (recomendado: `sudo apt install mpv`) — sin él no se reproduce
- Opcional: `python3-pil` para dibujar portadas reales (sin PIL hay un
  panel decorativo); `wl-copy`/`xclip` para copiar URLs al portapapeles

## Estructura

```
music-terminal            # lanzador ejecutable
music_terminal/
├── main.py               # CLI (region/lang), arranque
├── config.py             # rutas, preferencias, etiquetas
├── tui.py                # primitivas ANSI de anilinux (recoloradas) + kitty graphics
├── app.py                # la TUI: rail, pantallas, panel, diálogos, cola
├── cover.py              # portadas ANSI hi-res + PNG exacto para kitty
├── browser_cookies.py    # cookie de sesión de los navegadores (AES puro)
├── innertube.py          # cliente InnerTube + sesión Google (SAPISIDHASH)
├── extractor.py          # `player` → URL de audio (+ yt-dlp opcional)
├── streamproxy.py        # proxy local de rangos acotados (plan B)
├── downloader.py         # copias locales para oír sin internet
├── player.py             # mpv via socket IPC JSON (eventos, controles)
├── queue.py              # cola con repetición/mezcla/continuidad
└── database.py           # SQLite (canciones/favoritos/historial/playlists)
```

## Solución de problemas

- **«mpv no está instalado»** → `sudo apt install mpv`.
- **Sesión caducada / resultados raros** → Ajustes ▸ «Renovar sesión de
  YouTube» (borra y vuelve a pedir el visitorData).
- **La cuenta de Google deja de responder** → la cookie del navegador puede
  caducar: Ajustes ▸ Cuenta de Google ▸ cerrar sesión y vuelve a iniciarla
  (la cookie se recopia sola del navegador).
- **No se encuentra la cookie** → si el navegador usa un llavero protegido y
  `secret-tool` no lo resuelve, usa el camino manual (`m` en el diálogo).
- **403 al reproducir** → el flash informa del stream; prueba el extractor
  «Solo yt-dlp» en Ajustes (descarga ~80 MB una sola vez).
- **Sin conexión** → la TUI lo avisa al entrar a cada sección; reintenta
  con otra pestaña o espera y vuelve a abrir.

## Desinstalación

Cierra la app y fuera cada pieza por separado (y si queda un mpv en
segundo plano, este sale con su socket en `/tmp/music-terminal-mpv-…`):

```bash
pkill -f music-terminal-mpv        # mata un mpv que hubiera quedado en idle
rm ~/.local/bin/music-terminal     # fuera el lanzador
rm -rf /ruta/al/clon/music-terminal # fuera el código (donde hiciste git clone)
```

Borrar **también tus datos** es opcional — con esto se va TODO:

```bash
rm -rf ~/.config/music-terminal ~/.local/share/music-terminal
```

Piénsalo antes del último paso: se pierden para siempre las listas y
favoritos locales, el historial, la cola guardada, las portadas y las
**copias sin internet** (carpeta `offline/`), y la sesión de Google
(`auth.json`) se cierra en este equipo.  Si solo quieres desinstalar el
programa pero conservar la música descargada y tu biblioteca, borra solo
el lanzador y el clon, y deja las dos carpetas de datos quietas.
