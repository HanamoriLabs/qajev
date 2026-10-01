# qajev.com

The project's website: one static page (`index.html`, inline CSS and JS), its images in `assets/`, the privacy
policy (`privacy.html`, served at `/privacy`), `consent.js`, and `llms.txt`: plain-text setup and usage for AI
agents (check, install, connect the MCP server, use it), condensed from `AGENT_PROMPT.md` and `docs/mcp.md`. Keep
it in step with those two. No build step.

`consent.js` is the shared Hanamori cookie banner and Google Tag Manager loader (container `GTM-PWPQ2CK7`, set on
its `<script>` tag in both pages). Visitors in the EEA, the UK and Switzerland get a banner and nothing loads until
they accept; elsewhere Tag Manager loads after the page is idle, and Global Privacy Control keeps ads off. The
copy here comes from the hanamori-gtm repo: change it there, not here. `privacy.html` must list whatever the
container loads.

Preview it locally:

```bash
python3 -m http.server 8000 --bind 127.0.0.1 --directory site
```

It is served by Cloudflare Workers (static assets, no code), with `qajev.com` and `www.qajev.com` as custom
domains; `wrangler.jsonc` holds that setup and `.assetsignore` keeps repo-only files off the site. To deploy:

```bash
cd site && wrangler deploy
```

The screenshots come from `docs/images/` (resized to 1600 px wide, WebP). Replace them there first, then
regenerate the WebP copies.
