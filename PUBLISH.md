# Publish Nova so people can find it in Google / Bing

The site is static HTML. Search engines only index it **after** it is on a public URL.

## 1. Replace the placeholder domain

In these files, change every `YOUR-DOMAIN.example` to your real host:

- `index.html` (canonical, Open Graph, Twitter, JSON-LD)
- `robots.txt`
- `sitemap.xml`

Example: if the site will live at `https://aibel.dev/nova/`, use that base everywhere.

## 2. Host the folder (pick one)

| Host | How |
|------|-----|
| **GitHub Pages** | Create a public repo, push this folder, enable Pages (branch `main` / folder `/` or `/docs`). Free HTTPS URL like `https://username.github.io/nova-ai/`. |
| **Netlify / Cloudflare Pages** | Drag-and-drop the `nova-download-site` folder, or connect a Git repo. Free custom domain support. |
| **Your own domain** | Upload `index.html`, `robots.txt`, `sitemap.xml`, and `files/` via FTP or your host’s file manager. |

Keep this structure:

```
/
  index.html
  robots.txt
  sitemap.xml
  files/
    nova.py
    setup_nova.py
    setup_and_run.bat
    requirements.txt
    yolov8n.pt
    ...
```

## 3. Tell Google to index it

1. Open [Google Search Console](https://search.google.com/search-console)
2. Add your property (domain or URL prefix)
3. Submit the sitemap: `https://YOUR-DOMAIN/sitemap.xml`
4. Use **URL Inspection** → request indexing for the homepage

Bing: [Bing Webmaster Tools](https://www.bing.com/webmasters) → submit the same sitemap.

Indexing is not instant — often days to a few weeks. Clear titles and FAQ structured data help.

## 4. What people will search

The page is written so queries like these can match:

- Nova AI assistant
- free local voice AI Windows
- desktop voice assistant download
- Python voice assistant Hindi Malayalam
- offline AI assistant with memory

Share the link on Reddit, X, Discord, and product forums so real visits + backlinks speed up ranking.

## 5. Optional: custom domain

Point `nova.yourdomain.com` (or `yourdomain.com/nova`) at GitHub Pages / Netlify, then update canonical + sitemap URLs again and resubmit the sitemap.
