"""Seed reporting profiles. Profiles are stored in the database and editable by admins; this is
only the initial ATC profile derived from the real sample exports."""

ATC_PROFILE = {
    "name": "ATC - school campaigns (Meta)",
    "description": "Around The Clock Media: Brighton College / Bloom World Academy paid social. Derived from the September 2026 sample exports.",
    "default_currency": "AED",
    "default_timezone": "Asia/Dubai",
    # Approved column mappings: in ATC exports the custom 'Brand' column holds the creative name.
    "column_overrides": {"brand": "creative"},
    "naming": {
        "brand_codes": {"BWA": "Bloom World Academy", "BCD": "Brighton College Dubai",
                        "BCAD": "Brighton College Abu Dhabi", "BCAA": "Brighton College Al Ain"},
        "brand_phrases": {"Brighton-College-Dubai": "Brighton College Dubai", "Bloom-World": "Bloom World Academy"},
        "market_tokens": {"Dubai": "Dubai", "Abu-Dhabi": "Abu Dhabi", "Al-Ain": "Al Ain"},
        "brand_market": {"Bloom World Academy": "Dubai", "Brighton College Dubai": "Dubai",
                         "Brighton College Abu Dhabi": "Abu Dhabi", "Brighton College Al Ain": "Al Ain"},
        "market_country": {"Dubai": "UAE", "Abu Dhabi": "UAE", "Al Ain": "UAE"},
        "language_tokens": {"EN": "English", "AR": "Arabic"},
        "format_tokens": {"VID": "Video", "Video": "Video", "Static": "Static", "Carousel": "Carousel"},
    },
    "click_metric": "link_clicks",
    "approved_equivalences": [],
    "conversion_metric": None,  # not yet agreed with ATC - the app asks per task (and can save the answer here)
    "brand_order": ["Bloom World Academy", "Brighton College Dubai", "Brighton College Abu Dhabi", "Brighton College Al Ain"],
    "excel_tab_order": ["Executive Summary", "Creative Performance", "Brand Summary", "Platform Summary", "Campaign Performance",
                        "Market & Language", "Period Comparison", "Consolidated Data", "Insights", "Data Quality", "Methodology"],
    "deck": {
        "na_text": "-",
        "range_style": "ordinal",  # "1st – 21st Sep 2026"
        "client_content_markers": ["to be shared by the client", "kyra to update", "client to update"],
        "metric_labels": {"Views": "video_plays_3s", "Clicks": "clicks", "VTR": "vtr"},
    },
    "file_naming": "{brand}_{title}_{period}_v{version}",
}

GENERIC_PROFILE = {
    "name": "Generic paid social",
    "description": "Default settings for clients without a dedicated profile.",
    "column_overrides": {},
    "naming": {},
    "click_metric": "link_clicks",
    "approved_equivalences": [],
    "conversion_metric": None,
    "deck": {"na_text": "N/A", "range_style": "plain"},
    "file_naming": "{client}_{title}_{period}_v{version}",
}
