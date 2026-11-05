# Login background image

**Source:** original AI-generated image, created with Canva's image generator in Matheen's Canva
account on 5 Nov 2026 (Canva media `MAHaEr15r5E`) and supplied by Matheen as `width_1600.png`
(1600 x 899 px). It shows a fictional team in a warm, modern office (a presenter at a chart screen,
colleagues with laptops). The people are not real employees, and the image has no text or interface
elements in it.

Files: `app/static/img/login-hero-{960,1280,1600}.{webp,jpg}`. The source is 1600 px wide, so it is
not upscaled; on wider screens the browser scales the 1600 px file under the dark overlay.

The page lays a charcoal overlay (`.login-shade` in `app/static/app.css`) over the photo. It is darkest
behind the headline on the left and lighter towards the sign-in card, so the scene stays visible
while the text keeps enough contrast.

## How to replace it

1. Use a landscape image with no baked-in text, ideally at least 2400 px wide. Record its source here.
2. Export 960, 1280 and 1600 px widths (add 2400 if the source allows, and list it in the `srcset` and
   preload in `app/templates/login.html`) as WebP (quality ~74) and progressive JPEG (quality ~76).
3. If the focal point differs, adjust `object-position` on `.login-photo img`.
