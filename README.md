# mendeley-mcp-connector

> **🚧 Work in progress.** Usable, but young: tested on one Windows machine with one
> account. Interfaces may change before 1.0. Bug reports are welcome.

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) work
with **your own** Mendeley reference library: search it, add references by DOI or by hand,
organise them in folders, attach PDFs and export BibTeX.

Each person uses an API application that **they** register with **their own** Mendeley
account. The project ships no shared credentials, and nothing leaves your computer except
the requests to Mendeley and what your MCP client sends to its model.

> **Unofficial project.** This project is not affiliated with, endorsed by or sponsored by
> Mendeley or Elsevier. "Mendeley" is used only to describe what the software connects to.

## Contents

- [What it can do](#what-it-can-do)
- [Requirements](#requirements)
- [1. Register your Mendeley app](#1-register-your-mendeley-app)
- [2. Install](#2-install)
- [3. Log in](#3-log-in)
- [4. Configure Claude Desktop](#4-configure-claude-desktop)
- [Tools](#tools)
- [Command-line reference](#command-line-reference)
- [Security and privacy](#security-and-privacy)
- [Known limitations](#known-limitations)
- [Development](#development)
- [Status](#status)
- [License](#license)

## What it can do

- **Search** your library by words, title, author, year range or DOI (ignoring case and
  accents).
- **Add references by DOI** using the Mendeley catalog, refusing duplicates.
- **Add references by hand** for laws, bills, court cases, reports, web pages and anything
  without a DOI.
- **Folders**: list, create (without duplicates), file documents, list contents.
- **Attach a local PDF** to a document, without uploading the same file twice.
- **Export BibTeX** for a folder or a selection, optionally to a new `.bib` file.

**Nothing can be deleted.** There are no delete, remove or trash tools, and every tool is
marked as non-destructive for the MCP client.

## Requirements

- Python **3.11 or newer**.
- A Mendeley account.
- An MCP client. The instructions below use **Claude Desktop**.

## 1. Register your Mendeley app

You need your own application ID and secret. This takes two minutes and is free.

1. Go to <https://dev.mendeley.com/myapps.html> and sign in with your Mendeley account.
2. Choose **Register a new app** and fill in:
   - **Application name**: anything you like, e.g. `My MCP connector`. Avoid using
     "Mendeley" or Elsevier trademarks in the name.
   - **Description**: e.g. `Personal MCP server for my own library`.
   - **Redirect URL**: exactly `http://localhost:8585/callback`
3. Accept the terms, generate the **secret** and submit.
4. Write down the **application ID** (a number) and the **secret**.

> **Keep the secret secret.** Store it in a password manager. Never put it in a file inside
> a repository, in a screenshot or in a chat. If it leaks, generate a new one on the same
> page.

If port 8585 is taken on your computer, register a different redirect URL (for example
`http://localhost:8686/callback`) and pass it at login with `--redirect-uri`. It must match
the registered URL character for character.

## 2. Install

The package is not on PyPI yet. Install it from GitHub with
[pipx](https://pipx.pypa.io), which keeps it in its own environment:

```bash
pipx install git+https://github.com/victoriacarbonell/mendeley-mcp-connector
```

Or from a clone, in a virtual environment:

```bash
git clone https://github.com/victoriacarbonell/mendeley-mcp-connector
cd mendeley-mcp-connector
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install .
```

Check that it works:

```bash
mendeley-mcp-connector --version
```

## 3. Log in

```bash
mendeley-mcp-connector auth login
```

1. Paste your **application ID** when asked.
2. Paste your **secret** (the input is hidden).
3. Your browser opens the Mendeley login page. Sign in and allow access.
4. The browser shows a confirmation and the terminal prints `Connected`.

Credentials are stored in your system keyring (Windows Credential Manager, macOS Keychain,
or Secret Service on Linux). Access tokens last one hour and are refreshed automatically,
so you only log in once.

Then run the read-only checks:

```bash
mendeley-mcp-connector auth status
mendeley-mcp-connector doctor
```

`doctor` reports which API features work for your account. It never changes anything
and prints no personal data.

## 4. Configure Claude Desktop

1. Find the full path of the program:
   - Windows (PowerShell): `(Get-Command mendeley-mcp-connector).Source`
   - macOS/Linux: `which mendeley-mcp-connector`
2. In Claude Desktop, open **Settings → Developer → Edit Config**. This opens
   `claude_desktop_config.json`:
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
3. Add the server, using your own path:

   ```json
   {
     "mcpServers": {
       "mendeley": {
         "command": "C:\\Users\\YOUR_USER\\.local\\bin\\mendeley-mcp-connector.exe",
         "args": ["serve"]
       }
     }
   }
   ```

   On Windows, write every `\` in the path as `\\`. If the file already has other
   servers, add only the `"mendeley"` entry inside `mcpServers`.
4. Quit Claude Desktop completely (also from the system tray or menu bar) and open it
   again. The Mendeley tools appear in the tools menu.

No credentials go in this file: the server reads them from the keyring.

## Tools

Ask in plain language; Claude picks the tool. IDs come from earlier results, so you never
need to type them.

| Tool | What it does | Changes data? |
|---|---|---|
| `search_library` | Search your library by words, title, author, year, year range or DOI. | No |
| `get_document` | All stored details of one document, including attached files. | No |
| `add_document_by_doi` | Add a reference from the Mendeley catalog by DOI. Optional folder and document type. | Adds |
| `add_document_manual` | Add a reference from metadata typed by hand. | Adds |
| `list_folders` | List folders with their IDs and parents. | No |
| `create_folder` | Create a folder or subfolder; returns the existing one if the name is taken. | Adds |
| `add_to_folder` | File documents into a folder; skips those already in it. | Adds |
| `list_folder_documents` | List the documents in a folder. | No |
| `attach_pdf` | Upload a local PDF to a document; skips it if already attached. | Adds |
| `export_bibtex` | BibTeX for a folder or selected documents, returned or written to a new `.bib` file. | Writes a new local file only if asked |

### Examples

**Search**

> What do I have by Hinton between 2010 and 2016?

> Find the papers in my library about "transformer attention".

**Add by DOI**

> Add 10.1038/nature14539 to my Mendeley, in a folder called "Deep learning".

If a document with that DOI is already in your library, nothing is added and Claude is
told so. Pasting `https://doi.org/...` or `doi:...` also works.

**Add by hand**

> Add this law to Mendeley: "Example Act 12/2024 on fictional matters", published in the
> Official Gazette, 2024, issued by the Parliament of Exampleland.

Supported types: `journal`, `book`, `generic`, `book_section`, `conference_proceedings`,
`working_paper`, `report`, `web_page`, `thesis`, `magazine_article`, `statute`, `patent`,
`newspaper_article`, `computer_program`, `hearing`, `television_broadcast`,
`encyclopedia_article`, `case`, `film`, `bill`.

By default a document is refused if one with the same DOI, or the same title and year,
already exists. Claude can override this only by setting `allow_duplicate`, and the tool
description tells it to ask you first.

**Folders**

> Create a folder "Chapter 2" and put my 2023 papers on housing policy in it.

Filing a document in a folder never removes it from the library or from other folders.

**PDFs**

> Attach C:\Users\me\Downloads\paper.pdf to the Nature deep learning paper.

The file must be a real PDF (checked by its header) of at most 100 MB, on the computer
where the server runs. Only attach PDFs you have the right to store.

**BibTeX**

> Export the "Deep learning" folder as BibTeX to C:\Users\me\Documents\thesis.bib

The file must end in `.bib` and must not exist yet: existing files are never overwritten.
Without a path, the BibTeX is returned in the chat.

## Command-line reference

```text
mendeley-mcp-connector serve          Run the MCP server over stdio (used by Claude Desktop)
mendeley-mcp-connector auth login     Connect your Mendeley account
      --client-id ID                    Application ID (otherwise asked)
      --redirect-uri URL                Registered redirect URL (default http://localhost:8585/callback)
      --no-browser                      Print the login URL instead of opening the browser
mendeley-mcp-connector auth status    Show whether you are connected
mendeley-mcp-connector auth logout    Delete the stored credentials from this computer
mendeley-mcp-connector doctor         Read-only checks of which API features work
mendeley-mcp-connector --verbose ...  Debug logging (to stderr)
```

Optional environment variables:

| Variable | Purpose |
|---|---|
| `MENDELEY_CLIENT_ID` | Application ID for `auth login` |
| `MENDELEY_CLIENT_SECRET` | Secret for `auth login`. Prefer typing it at the prompt, which keeps it out of shell history. |
| `MENDELEY_REDIRECT_URI` | Redirect URL for `auth login` |
| `MENDELEY_MCP_TOKEN_BACKEND` | `auto` (default), `keyring` or `file` |
| `MENDELEY_MCP_CONFIG_DIR` | Override the configuration directory |

## Security and privacy

- **Your own app, your own credentials.** The repository contains no application ID,
  secret or token.
- **Keyring storage.** The secret, access token and refresh token are stored in the system
  keyring. Only non-secret settings (application ID, redirect URL, expiry time) go in
  `config.json` in the configuration directory:
  - Windows: `%APPDATA%\mendeley-mcp-connector`
  - macOS: `~/Library/Application Support/mendeley-mcp-connector`
  - Linux: `~/.config/mendeley-mcp-connector`
- **Fallback file.** If no keyring is available, secrets go in `secrets.json` in the same
  directory, written with owner-only permissions on macOS and Linux. On Windows the file
  relies on your user profile's permissions.
- **Login checks.** The login listens only on `127.0.0.1` and checks the OAuth `state`
  value to reject forged callbacks.
- **Credentials stay with Mendeley.** The client refuses to send them to any URL outside
  `https://api.mendeley.com`.
- **Logging out.** `auth logout` deletes everything stored locally. To revoke access
  completely, also regenerate the secret at <https://dev.mendeley.com/myapps.html>.
- **What the model sees.** The metadata returned by the tools (titles, authors, abstracts,
  folder names) is sent to your MCP client and its model, under that provider's terms. PDF
  contents are never read or sent to the model.

## Known limitations

- **Unofficial.** It uses the public Mendeley API, which Mendeley may change or restrict
  without notice. If something stops working, run `doctor` first.
- **Terms of use.** You are responsible for using it within the
  [Mendeley Terms of Use](https://www.mendeley.com/terms/) and Elsevier's terms. This tool
  is meant for personal research use of your own library, not for bulk or automated
  harvesting.
- **PDF rights.** Only upload files you are allowed to store.
- **No edits, no deletions.** Documents can be added, filed and exported, but not edited,
  removed from folders or deleted. Do that in Mendeley itself.
- **Search runs locally.** The `/documents` endpoint ignores its `doi` filter (confirmed
  with `doctor`), and the behaviour of the search endpoint is not documented in enough
  detail. So each search downloads your library's metadata, 500 documents per request,
  and filters it on your computer. This is reliable but slower for very large libraries.
- **Document types from the catalog.** The catalog reports some journal articles as
  `generic`. Records with a journal name are saved as `journal`, and Claude is told when
  that happens. You can also give the type explicitly.
- **One account per computer.** Logging in again replaces the stored account.
- **stdio only.** The server runs locally for desktop clients. The code is organised so
  that an HTTP transport can be added later, but there is none yet.
- **Not yet confirmed against the live API:** creating subfolders (`parent_id`), and
  whether file hashes are returned. Without hashes, duplicate PDF detection compares file
  name and size.

## Development

```bash
pip install -e ".[dev]"
pytest            # all HTTP is mocked with respx; tests never contact Mendeley
ruff check .
ruff format --check .
mypy
```

CI runs lint, type checks and tests on Windows, macOS and Linux with Python 3.11 and 3.13.
Tests use only invented data. Please keep it that way, and never commit credentials.

Project layout:

```text
src/mendeley_mcp_connector/
  auth/        OAuth login and token storage
  api/         HTTP client: refresh, retries, pagination
  services/    Library logic (documents, folders, PDFs, BibTeX)
  tools.py     MCP tool definitions
  server.py    MCP server factory
  transports/  stdio (an HTTP transport can be added here)
  cli.py       Command-line interface
  doctor.py    Read-only API checks
```

## Status

- [x] OAuth 2.0 login with keyring storage and automatic refresh
- [x] HTTP client with rate-limit retries and pagination
- [x] `doctor` command
- [x] Library tools (no deletion)
- [x] Tested with Claude Desktop on Windows: create folder, add by DOI, list folder
- [ ] Tested with a real account: manual add, PDF upload, BibTeX export, subfolders
- [ ] Tested with a real account on macOS and Linux (CI only so far)
- [ ] Published on PyPI

## License

[MIT](LICENSE) © Victoria Carbonell Sánchez
