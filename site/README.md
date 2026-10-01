# qajev.com

The project's website: one static page (`index.html`, inline CSS and JS) and its images in `assets/`. No build
step.

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
