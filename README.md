# Tabla Compositions

A self-contained database + HTML UI for storing your tabla compositions
(Kaida, Tukda, Rela, Chakradar, …).

## Run

```bash
cd /Users/pirhoalphadelta/AI:ML
pip3 install -r requirements.txt     # only first time
python3 app.py
```

Then open **http://localhost:5050** in a browser.

- Data lives in `compositions.db` (SQLite).
- Uploaded images/PDFs live in `uploads/`.
- Both are local; nothing leaves your machine.

## What it stores

For every composition:

| Field           | Required | Notes                                       |
|-----------------|----------|---------------------------------------------|
| Type            | yes      | Kaida, Tukda, … (add new ones from the UI)  |
| Name            | yes      | The composition's name                      |
| Taal            | yes      | Free text — suggestions appear as you add   |
| Speed group     | yes      | e.g. Vilambit / Madhya / Drut — also free    |
| Bol type        | no       |                                             |
| Gharana         | no       |                                             |
| Miscell info    | no       | Any notes                                   |
| Attachments     | no       | Notebook photos, PDFs (up to 10 MB each)    |

## Use

- **Add composition** — top-right button, or press `n`.
- **View / edit** — click any row to open the detail drawer; press `Esc` to close.
- **Filter** — type chips for composition type, dropdowns for taal & speed, free-text search (`/`).
- **Attachments** — open a row, then drop or pick a JPG / PNG / WebP / PDF.
- **Export CSV** — top-right button. Exports the currently filtered set.
- **Add composition type** — `+ type` chip in the filter row.
- **Dark mode** — ☾/☀ button in the header.

## Standalone mode

`index.html` also works opened directly from disk (no server). The page detects
`file://` and shows a banner asking for the server URL. Type
`http://localhost:5050` and click **Connect** — same data, same API, just
opened from anywhere.

## File map

```
app.py            Flask + SQLite backend, all REST routes
index.html        The entire UI (one file, no build step)
compositions.db   Created on first run
uploads/          Created on first upload
```
