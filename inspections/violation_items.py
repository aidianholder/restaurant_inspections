"""The 57 numbered items on the Arkansas food establishment inspection form.

Each violation on a report cites one of these numbers, and the number maps to a
short description of what the item covers — the text readers see before they open
the inspector's notes.

Derived from page 1 of the reports themselves rather than typed by hand: the
checklist is printed on every report, so the list was extracted from 188 of them
and cross-checked, with each number required to yield identical text across all.
Only item 26 disagreed, where 11 reports bled a stray glyph into the cell; the
majority text is used.

Item 57 is the catch-all ("Other violations"), so its official wording is not
useful to a reader — display it as "Other violations" and let the code citation
and inspector comments carry the meaning.

Seeded into the ViolationItem table by a data migration. Editing this file does
not change the database; re-run `manage.py seed_violation_items` for that.
"""

# (number, section, subsection, official description)
VIOLATION_ITEMS = [
    (1, "RISK_FACTORS", "Supervision", "Person in charge present, demonstrates knowledge, and performs duties"),
    (2, "RISK_FACTORS", "Supervision", "Certified Food Protection Manager"),
    (3, "RISK_FACTORS", "Employee Health", "Management, food employee and conditional employee; knowledge, responsibilities, and reporting"),
    (4, "RISK_FACTORS", "Employee Health", "Proper use of restriction and exclusion"),
    (5, "RISK_FACTORS", "Employee Health", "Clean-Up of Vomiting and Diarrheal Events"),
    (6, "RISK_FACTORS", "Good Hygienic Practices", "Proper eating, tasting, drinking, or tobacco use"),
    (7, "RISK_FACTORS", "Good Hygienic Practices", "No discharge from eyes, nose, and mouth"),
    (8, "RISK_FACTORS", "Preventing Contamination by Hands", "Hands clean & properly washed"),
    (9, "RISK_FACTORS", "Preventing Contamination by Hands", "No bare hand contact with RTE foods or approved alternate method properly followed"),
    (10, "RISK_FACTORS", "Preventing Contamination by Hands", "Adequate handwashing facilities supplied & accessible"),
    (11, "RISK_FACTORS", "Approved Source", "Food obtained from approved source"),
    (12, "RISK_FACTORS", "Approved Source", "Food received at proper temperature"),
    (13, "RISK_FACTORS", "Approved Source", "Food in good condition, safe and unadulterated"),
    (14, "RISK_FACTORS", "Approved Source", "Required records available: shellstock tags, parasite destruction"),
    (15, "RISK_FACTORS", "Protection From Contamination", "Food separated/protected"),
    (16, "RISK_FACTORS", "Protection From Contamination", "Food-contact surfaces: cleaned and sanitized"),
    (17, "RISK_FACTORS", "Protection From Contamination", "Proper disposition of returned, previously served, reconditioned & unsafe food"),
    (18, "RISK_FACTORS", "Potentially Hazardous Food Time/Temperature", "Proper cooking time and temperatures"),
    (19, "RISK_FACTORS", "Potentially Hazardous Food Time/Temperature", "Proper reheating procedures for hot holding"),
    (20, "RISK_FACTORS", "Potentially Hazardous Food Time/Temperature", "Proper cooling time and temperatures"),
    (21, "RISK_FACTORS", "Potentially Hazardous Food Time/Temperature", "Proper hot holding temperatures"),
    (22, "RISK_FACTORS", "Potentially Hazardous Food Time/Temperature", "Proper cold holding temperatures"),
    (23, "RISK_FACTORS", "Potentially Hazardous Food Time/Temperature", "Proper date marking and disposition"),
    (24, "RISK_FACTORS", "Potentially Hazardous Food Time/Temperature", "Time as a public health control; procedures & record"),
    (25, "RISK_FACTORS", "Consumer Advisory", "Consumer advisory for raw or undercooked foods"),
    (26, "RISK_FACTORS", "Highly Susceptible Populations", "Pasteurized foods used; prohibited foods not offered"),
    (27, "RISK_FACTORS", "Chemical", "Food additives; approved & properly stored"),
    (28, "RISK_FACTORS", "Chemical", "Toxic substances properly identified, stored, & used"),
    (29, "RISK_FACTORS", "Conformance with Approved Procedures", "Compliance with variance, specialized process, & HACCP plan"),
    (30, "GOOD_RETAIL_PRACTICES", "Safe Food and Water", "Pasteurized eggs used where required"),
    (31, "GOOD_RETAIL_PRACTICES", "Safe Food and Water", "Water and ice from approved source"),
    (32, "GOOD_RETAIL_PRACTICES", "Safe Food and Water", "Variance obtained for specialized processing methods"),
    (33, "GOOD_RETAIL_PRACTICES", "Food Temperature Control", "Proper cooling method used; adequate equipment used for temperature control"),
    (34, "GOOD_RETAIL_PRACTICES", "Food Temperature Control", "Plant food properly cooked for hot holding"),
    (35, "GOOD_RETAIL_PRACTICES", "Food Temperature Control", "Approved thawing methods used"),
    (36, "GOOD_RETAIL_PRACTICES", "Food Temperature Control", "Thermometers provided & accurate"),
    (37, "GOOD_RETAIL_PRACTICES", "Food Identification", "Food properly labeled; original container"),
    (38, "GOOD_RETAIL_PRACTICES", "Prevention of Food Contamination", "Insects, rodents & animals not present; no unauthorized persons"),
    (39, "GOOD_RETAIL_PRACTICES", "Prevention of Food Contamination", "Contamination prevented during food preparation, storage/display"),
    (40, "GOOD_RETAIL_PRACTICES", "Prevention of Food Contamination", "Personal cleanliness"),
    (41, "GOOD_RETAIL_PRACTICES", "Prevention of Food Contamination", "Wiping cloths: properly used and stored"),
    (42, "GOOD_RETAIL_PRACTICES", "Prevention of Food Contamination", "Washing fruits and vegetables"),
    (43, "GOOD_RETAIL_PRACTICES", "Proper Use of Utensils", "In-use utensils: properly stored"),
    (44, "GOOD_RETAIL_PRACTICES", "Proper Use of Utensils", "Utensils, equipment & linens: properly stored, dried & handled"),
    (45, "GOOD_RETAIL_PRACTICES", "Proper Use of Utensils", "Single-use & single-service articles: properly stored & used"),
    (46, "GOOD_RETAIL_PRACTICES", "Proper Use of Utensils", "Gloves used properly"),
    (47, "GOOD_RETAIL_PRACTICES", "Utensils, Equipment and Vending", "Food & non-food contact surfaces cleanable, properly designed, constructed & used"),
    (48, "GOOD_RETAIL_PRACTICES", "Utensils, Equipment and Vending", "Warewashing facilities: installed, maintained, used; test strips"),
    (49, "GOOD_RETAIL_PRACTICES", "Utensils, Equipment and Vending", "Non-food contact surfaces clean"),
    (50, "GOOD_RETAIL_PRACTICES", "Physical Facilities", "Hot and cold water available; adequate pressure"),
    (51, "GOOD_RETAIL_PRACTICES", "Physical Facilities", "Plumbing installed; proper backflow devices"),
    (52, "GOOD_RETAIL_PRACTICES", "Physical Facilities", "Sewage & waste water properly disposed"),
    (53, "GOOD_RETAIL_PRACTICES", "Physical Facilities", "Toilet facilities: properly constructed, supplied and cleaned"),
    (54, "GOOD_RETAIL_PRACTICES", "Physical Facilities", "Garbage and refuse properly disposed; facilities maintained"),
    (55, "GOOD_RETAIL_PRACTICES", "Physical Facilities", "Physical facilities installed, maintained and cleaned"),
    (56, "GOOD_RETAIL_PRACTICES", "Physical Facilities", "Adequate ventilation and lighting; designated areas used"),
    (57, "GOOD_RETAIL_PRACTICES", "Physical Facilities", "Other violations: Code Number must be noted on following page."),
]

CATCH_ALL_NUMBER = 57
