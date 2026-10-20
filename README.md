# WARS

A small, interactive manga review and community app. Python's standard library serves the site and JSON API; SQLite stores accounts, library entries, reviews, shelves, chat rooms, conversations, and messages.

## Run it

1. Install Python 3.10 or newer if it isn't already installed.
2. Open a terminal in this folder and run `py app.py` (or `python app.py`).
3. Open [http://127.0.0.1:8001](http://127.0.0.1:8001).

The app creates `panel_wars.sqlite3` beside `app.py` the first time it starts. It uses port `8001` by default; set `PORT` to use a different port and `HOST` to change the bind address. For local development, it binds to `127.0.0.1`; for a public host, set `HOST=0.0.0.0`. Set `COOKIE_SECURE=true` when HTTPS is enabled.

New signups create a reader account. The owner can promote registered users from the Roles screen. Admin and owner accounts are omitted from the reader directory, and the server rejects DMs to or from admin and owner accounts. Readers and moderators can message each other.

## Included

- Search and genre filters for the fictional library. Owners and editors can add or remove manga from the Discover page; removed titles are archived so their reviews and shelf records remain intact.
- A per-user shelf with reading status and chapter progress.
- One editable rating and review per reader per manga.
- Three live chat rooms. New messages are persisted in SQLite and broadcast to connected readers over Server-Sent Events.
- Private reader/moderator conversations with server-side role checks.
- Role controls: owners can assign reader, room moderator, community moderator, editor, or admin roles and can ban or unban accounts. Admins can promote or demote moderators; community moderators can promote or demote room moderators.
- Owner background events support the built-in themes or a custom JPEG, PNG, or WebP image up to 5 MB. Custom images are stored in SQLite and shown to site visitors.
- Chat moderation: room moderators can mute readers in a room for one hour. Owners can mute readers across all rooms and can unmute accounts. Owner accounts and each signed-in staff member's own role are protected.
- The site footer includes a feedback form that saves suggestions and problem reports in SQLite for the owner to review from Roles.
- Password hashing, signed-in session cookies, and a responsive layout.

## Public hosting on Vercel

Vercel serves the website files, while the Python API runs as a persistent Render web service. WARS uses SQLite for accounts, reviews, chat, and uploaded pages; the live chat also keeps connections in the running server process. Keep that backend on a persistent service rather than putting the SQLite file inside a Vercel deployment.

`vercel.json` forwards `/api/*` requests to `https://wars.onrender.com`, matching the service name in `render.yaml`. If Render gives the service a different URL, update the rewrite destination to that exact HTTPS origin before deploying the Vercel project. Vercel keeps the browser URL on the Vercel domain while proxying API calls to Render.

1. Push this project folder to a Git provider, then connect that repository to Render and deploy the `render.yaml` Blueprint. Review the Starter service and persistent disk plan before confirming; they may incur charges.
2. Copy the Render service's HTTPS URL. If it differs from `https://wars.onrender.com`, update `vercel.json` as described above.
3. Import the same repository into Vercel and set the project root to this folder. The checked-in `vercel.json` runs `node build.mjs` and publishes the generated `dist/` folder.
4. Deploy the Vercel project. Its `*.vercel.app` address serves the site, and its `/api/*` requests reach the Render backend.
5. The Render database starts empty unless you privately transfer `panel_wars.sqlite3` to its persistent disk. That file contains password hashes and private messages; never commit it to Git. A new database will not include the current owner account.

The Vercel URL works without owning a custom domain. You can add one later from Vercel after registering it.
## Add and read manga pages

Owners and editors can attach JPEG, PNG, or WebP scans from **Discover → Manage fictional library**. Choose the chapter number, select multiple page images, then upload. Files are ordered by filename (use names such as `001.jpg`, `002.jpg`, and so on). Each image can be up to 5 MB, with up to 100 pages per upload. Open the manga card to read uploaded chapters; the viewer shows pages in order.
