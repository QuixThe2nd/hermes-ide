# Website

This website is built using [Docusaurus](https://docusaurus.io/), a modern static website generator.

> **Reading the docs on GitHub?** The Markdown under `docs/` is authored for the rendered site at
> <https://hermes-agent.nousresearch.com/docs/>. Most cross-page links are Docusaurus site routes
> (`/getting-started/installation`), which GitHub's file viewer resolves as repository paths and 404s.
> Every page has an **Edit this page** link on the site that opens the source file here.

## Authoring links in `docs/`

- Link to another page with a doc-root route (`[Profiles](/user-guide/profiles)`) or a relative
  Markdown path (`[Profiles](../user-guide/profiles.md)`). Both render on the site; only the
  relative form also resolves on GitHub.
- Never write `/docs/...` in a link: `baseUrl` is already `/docs/`, so the zh-Hans build emits
  `/docs/zh-Hans/docs/...` 404s.
- Cross-section links with an anchor use the route form (`/section/page#anchor`); pin `{#anchor}`
  on the target heading so the zh-Hans mirror keeps the same id.

## Installation

```bash
yarn
```

## Local Development

```bash
yarn start
```

This command starts a local development server and opens up a browser window. Most changes are reflected live without having to restart the server.

## Build

```bash
yarn build
```

This command generates static content into the `build` directory and can be served using any static contents hosting service.

## Deployment

Using SSH:

```bash
USE_SSH=true yarn deploy
```

Not using SSH:

```bash
GIT_USER=<Your GitHub username> yarn deploy
```

If you are using GitHub pages for hosting, this command is a convenient way to build the website and push to the `gh-pages` branch.

## Diagram Linting

CI runs `ascii-guard` to lint docs for ASCII box diagrams. Use Mermaid (````mermaid`) or plain lists/tables instead of ASCII boxes to avoid CI failures.
