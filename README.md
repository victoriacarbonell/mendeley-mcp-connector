# mendeley-mcp-connector

> **🚧 Work in progress.** Not ready for general use yet. Interfaces may change.

An [MCP](https://modelcontextprotocol.io) server that connects Claude (or any MCP client) to
**your own** Mendeley reference library, using an API application that **you** register with
your own Mendeley account.

> **Unofficial project.** This project is not affiliated with, endorsed by or sponsored by
> Mendeley or Elsevier. "Mendeley" is used only to describe what the software connects to.

## Status

- [x] OAuth 2.0 login (`auth login / status / logout`) with tokens stored in the system keyring
- [x] HTTP client with automatic token refresh, rate-limit retries and pagination
- [x] `doctor` command: read-only checks of which API features work for your account
- [x] Library tools: search, add by DOI or by hand (no duplicates), folders, PDF upload,
      BibTeX export. No tool can delete anything.
- [ ] Tested end to end with Claude Desktop
- [ ] Full documentation

Full installation and usage instructions will be added before the first release.

## License

[MIT](LICENSE) © Victoria Carbonell Sánchez
