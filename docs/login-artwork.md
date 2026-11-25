# Login page artwork

The sign-in page is a split: a charcoal stage on the left carrying the headline and
an office photograph, the sign-in column on warm paper at the right. Both pieces of
artwork below are approved and final — they are not placeholders.

## Office photograph

**Source:** supplied by Matheen on 26 Nov 2026 in `DataPlus-Claude-Handoff`, as the
standalone analytics-office image (1672 x 941). The scene is a dark modern office at
dusk with a monitor showing a campaign dashboard. The people, company and figures in
it are fictional.

The monitor shows October 2026 vs September: $125,000 revenue (+25%), $25,000 ad spend
(flat), 5.0x ROAS (+1.0x), and weekly revenue of $25k / $28k / $32k / $40k, with a
**"Demo data"** label in the corner. Those numbers are illustrative and exist only
inside the photograph — nothing on the page repeats them as a claim. Keep the "Demo
data" label in any replacement.

Files: `app/static/img/login-office-{960,1280,1600}.{webp,jpg}`.
Unmodified source: `docs/source/login-office-source.webp`.

The source is 1672 px wide, so the 1600 px export is not upscaled. The image sits
inside the left panel rather than behind the whole page: `.ls-photo img` in
`app/static/app.css` fades its top edge into the charcoal with a `mask-image`, so the
picture emerges from the panel instead of sitting on it as a pasted box.

### Replacing it

1. Use a landscape image, at least 1600 px wide, with no text outside an illustrative
   screen. Record its source here.
2. Export 960, 1280 and 1600 px widths as WebP (quality ~74) and progressive JPEG
   (quality ~76), and keep the untouched source in `docs/source/`.
3. If the focal point differs, adjust `object-position` on `.ls-photo img`.
4. Below 360 px the photograph is hidden: at that width it pushes the sign-in button
   off the first screen. Above that it follows the sign-in column.

## Data+ wordmark

`app/static/img/dataplus-primary.png` — black lettering with a red plus, cropped to
transparency from the approved Data+ identity sheet (same handoff). The lettering is
drawn, not set in a font: the D carries a diagonal cut and the plus is raised. The
website header uses the reversed white version of the same artwork, kept in the
website repository at `public/brand/dataplus-white.png`.

It is `alt="Data+"` here, because on this page the wordmark is the only thing naming
the product. In the website header the equivalent image is `alt=""`, because the
anchor there already carries the accessible name "Log in to Data+".
