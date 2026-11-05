# Login background image

**Status: temporary placeholder. A licensed photograph is still needed for final delivery.**

The login page shows a full-screen photo behind a layered charcoal overlay. The files in
`app/static/img/login-hero-{960,1600,2400}.{webp,jpg}` are a *generated* placeholder: an abstract,
heavily blurred warm office scene (window light, pendant lamps, wall screens), made by
`scripts/make_login_placeholder.py`. It has no people or real location in it and no licensing
restrictions, but it is not the final asset.

The build environment could not reach stock-photo hosts, so no licensed photograph was downloaded.

## Asset needed

- A licensed or company-owned photo of a contemporary digital media team collaborating in an office
  (laptops, screens with charts, natural poses). Warm, subdued light. Faces not in sharp focus.
- Landscape, at least 2400 x 1500 px. Keep the left ~45% calm and darker (the headline sits there).
- Record the source and licence here when it is added.

## How to replace it

1. Soften it slightly (Gaussian blur about 6-10 px at 2400 px wide) if it is not already out of focus.
2. Export each width (960, 1600, 2400) as WebP (quality ~72) and progressive JPEG (quality ~74),
   using the same file names. Aim for under 250 KB at 2400 px.
3. No template or CSS change is needed. If the focal point differs, adjust `object-position`
   on `.login-photo img` in `app/static/app.css`.
