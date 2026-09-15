"""
Seed script — drops and recreates all tables, then seeds:
  - SUPER_ADMIN user
  - 192 plant locations
  - 49 designations (with default 30-day notice period)
  - All 34+ form fields with options
"""
from app import create_app
from app.extensions import db, bcrypt
from app.models import (
    User, UserRole, PlantLocation, Designation,
    FormField, FormFieldOption, FieldType, OptionsSource
)

app = create_app()

# ── Data ───────────────────────────────────────────────────────────────────────

PLANTS = [
    "ULT - Kol Howrah", "ULT - Nagpur", "ULT - Surat", "ULT - Vizag", "ULT-Goa",
    "ULT-Nellore", "ULT-Raipur", "ULT-Wada", "ULT-Pallava", "BG-Mysore Road",
    "BG-Veerasandra", "BG-Yelhanka", "BG-Budigere", "BG-Hedge Nagar", "BG-Anjanapura",
    "BG-Tumkur Road", "BG-Whitefield", "BG-Devenahalli", "BG-Doddaballapura",
    "BG-Kalpataru", "BG-Lodha Heaven", "CHE-Pudupakkam", "CHE-Ambattur",
    "CHE-Madhavaram", "CHE-Medavakkam", "CHE-Oragadam", "CHE-Poonamallee",
    "CHE-ZOHO", "CHE - Casa Hola", "TN-Hosur", "TN-Coimbatore 1", "TN-Coimbatore 2",
    "TN-Trichy", "HYD-Bachupally", "HYD-Khanapur", "HYD-Kollur", "HYD-Medchal",
    "HYD-Nacharam", "HYD-Patancheru", "HYD-Satva", "HYD-Tellapur", "HYD-Tukkuguda",
    "HYD-Vasavi Narsingi", "Hyderabad-Rajendra Nagar", "HYD-Kokapet", "KAR-Mangalore",
    "KER-Kochi 1 Edyar", "KER-Kochi 2 Amb.", "KER-Kottayam", "KER-Thrissur 1",
    "KER-Thrissur 2", "KER-Trivandrum 1", "KER-Trivandrum 2", "KER-Trivandrum 3",
    "KER-Kozhikode", "KER - Wayanad", "KOL-Hooghly", "KOL-Howrah", "KOL-New Town",
    "KOL-Taratala", "KOL-Kalyani Exp", "BHR-Captive", "BHR-Patna Comm",
    "WB-GDCL Farakka", "WB-Sagardighi", "ASM-Dalmia Lanka", "ASM-Dalmia Umrangso",
    "ASM-Guwahati", "MUM-Deonar", "MUM-Kalyan", "MUM-Kashimira", "MUM-Khalapur",
    "MUM-Kharghar", "MUM-Mankoli", "MUM-Pallava", "MUM-Sakinaka", "MUM-Turbhe",
    "MUM-Worli", "MUM-Raheja", "MUM-Uran", "MUM-Taloja", "MUM-Mahalaxmi",
    "MH-Nagpur 1", "MH-Nagpur 2", "CHA-Raipur 1", "CHA-Durg", "CHA-Raipur 2",
    "CHA-BHEL Bhillai", "MP-Bhopal 1", "MP-Bhopal 2", "MP-Indore", "MP-Indore 2",
    "MP-Satna KEC", "MP-Jabalpur", "GOA-Goa 1", "GOA-Goa 2", "GUJ-Surat ITD Hazira",
    "GUJ-Surat 2", "GUJ-Surat GLM", "GUJ-Surat 1", "GUJ-Surat TPL  Hazira",
    "GUJ-Vadodara - L&T", "GUJ-Vadodara - Dumad", "GUJ-ITD Mundra",
    "GUJ-Mundra Comm", "GUJ-Jamnagar", "GUJ - Ahmedabad Sanand",
    "GUJ - Ahmedabad Gift City", "NCR-Aerocity", "NCR-Gurgaon Badshahpur",
    "NCR-Faridabad", "NCR-Greater Noida 1", "NCR-Greater Noida 2", "NCR-Gurgaon",
    "NCR-Gurgaon Kherki", "NCR-Gurgaon Shikohpur M3M 1", "NCR- Gurgaon M3M 2",
    "NCR-Ghewra", "NCR-Gurugram Classic", "HYA-Sonipat", "JK-Jammu",
    "PB-Derabassi", "PB-Ludhiana", "PB-Mohali", "PB-Kurali", "UP-Lucknow",
    "UP-Gorakhpur", "UP-Ayodhya", "RAJ-Ambuja", "ODI-Bhubaneswar 2",
    "ODI-Cuttack Comm", "ODI-Teknow", "ODI-TPL Talcher", "ODI-BHEL Talcher",
    "ODI-GDCL Jajpur", "ODI-KEC Jajpur", "JHA-Ranchi", "JHA-Jamshedpur",
    "PUN-Hadapsar", "PUN-Hinjewadi", "PUN-Jambe", "PUN-TPL Metro", "PUN-TPL Metro 2",
    "PUN-Sinhgarh", "Head office", "Raj-Jaipur", "ROBO SILICON", "Panagarh-Crusher unit",
    "Panagarh-MS", "Robo-AP_RO", "Robo-Girmapur Plant", "Robo-Girmapur Plant 2",
    "Robo-Keesara_4 Plant", "Robo-Lakdaram_Plant", "Robo-Mudibidri_Plant",
    "Robo-RDU_Deshmukh", "Robo-RDU_Lakdaram", "Robo-RO_Bangalore",
    "Robo-Solakpally_Plant", "HYD-Rajender Nagar", "MUM-Shital Baug", "BG-Hosur",
    "CHE-Tirusulam", "BG-Lodha", "MH-Aurangabad", "ASM-Guwahati-2", "MUM-Vikhroli",
    "AP-Vizag", "AP - Vijaywada 2", "HYD-Kalpataru", "CHE-Asia",
    "PUN-Lodha Captive", "CHE-Saidapet", "KOL-Kalyani", "NCR Gurgaon-Daulatabad",
    "BG-Bagluru", "BG-Belgavi", "GUJ- Suvali Hazira", "ASM-Guwahati-3",
    "UK - Haridwar", "UK - Dehradun", "Robo-Khajipalli plant", "Robo-Rachalur Plant",
    "NCR-Greater Noida 3", "NCR - Kharkhoda", "JK-Kathua", "PB-Ludhiana 2",
    "KOL-KHARDAH", "PB-Ludhiana 3", "GUJ-Vapi", "GUJ-RIL Jamnagar", "UP-Jewar",
]

DESIGNATIONS = [
    "Batching Plant Operator", "Loader / JCB / Bull Trackter Operator",
    "Trainee Engineer", "Officer - Technical", "Field Technician", "Assistant",
    "Boom Pump Helper", "Technician Apprentice", "Trainee RMX Technician",
    "Officer - Safety", "TM Driver", "Executive Systems",
    "Customer Relationship Officer", "Executive Accounts", "Pump Operator",
    "Lab Technician", "Senior Field Technician", "Executive Dispatch & Logistics",
    "Boom Pump Operator cum Driver", "Executive Materials", "RMX Technician",
    "Dispatcher", "Executive Logistics", "Officer Sales", "Trainee Accounts",
    "Executive - Credit Control", "Trainee - Batching Plant Operator",
    "Officer Production", "Tipper Driver", "CRO cum Line Pump Operator", "OSE",
    "TM Supervisor", "Executive HR", "Electrician", "Scrapper Operator",
    "Mess caretaker", "Data Entry Operator", "CDS", "Executive Operations",
    "Pump Operator Cum CRO", "Trainee - Sales", "Officer - sales Executive",
    "Admin", "Trainee Project", "Senior Executive Logistics", "Officer Logistics",
    "Senior Officer Sales", "Welder cum Operator", "Trainee - Logistics",
]

# (field_key, field_label, field_type, step, is_required, options_source, allow_other,
#  is_readonly, placeholder, help_text, options_list)
FORM_FIELDS = [
    # ── Step 1: Personal Information ──────────────────────────────────────────
    ("company_code", "Company Code", FieldType.DROPDOWN, 1, True, OptionsSource.INLINE, False, False,
     "Select company", None,
     [("RDC", "RDC"), ("Ultrafine", "Ultrafine"), ("ROBO", "ROBO")]),

    ("associate_name", "Associate Name (as per Aadhar)", FieldType.TEXT, 1, True, OptionsSource.INLINE, False, False,
     "Full name as on Aadhar card", "Must exactly match Aadhar ID", []),

    ("father_name", "Father Name", FieldType.TEXT, 1, True, OptionsSource.INLINE, False, False,
     "Father's full name", None, []),

    ("gender", "Gender", FieldType.RADIO, 1, True, OptionsSource.INLINE, False, False,
     None, None,
     [("Male", "Male"), ("Female", "Female")]),

    ("date_of_birth", "Date of Birth", FieldType.DATE, 1, True, OptionsSource.INLINE, False, False,
     None, "Must be accurate — no corrections allowed after submission.", []),

    ("qualification", "Qualification", FieldType.DROPDOWN, 1, True, OptionsSource.INLINE, True, False,
     "Select qualification", None, [
        ("Below SSC / 10th", "Below SSC / 10th"),
        ("SSC / 10th", "SSC / 10th"),
        ("HSC / 12th", "HSC / 12th"),
        ("ITI", "ITI"),
        ("B.COM", "B.COM"),
        ("BA", "BA"),
        ("B.Sc", "B.Sc"),
        ("M.Com", "M.Com"),
        ("MA", "MA"),
        ("M.Sc", "M.Sc"),
        ("Diploma - Civil", "Diploma - Civil"),
        ("Diploma - Mechanical", "Diploma - Mechanical"),
        ("Diploma - Electrical", "Diploma - Electrical"),
        ("BE/B.Tech - Civil", "BE/B.Tech - Civil"),
        ("BE/B.Tech - Mechanical", "BE/B.Tech - Mechanical"),
        ("BE/B.Tech - Electrical", "BE/B.Tech - Electrical"),
        ("M.Tech", "M.Tech"),
    ]),

    ("permanent_address", "Permanent Address", FieldType.TEXTAREA, 1, True, OptionsSource.INLINE, False, False,
     "Full permanent residential address", None, []),

    ("pincode", "Pincode", FieldType.TEXT, 1, True, OptionsSource.INLINE, False, False,
     "6-digit pincode", None, []),

    ("mobile_number", "Mobile Number", FieldType.TEL, 1, True, OptionsSource.INLINE, False, False,
     "10-digit mobile number", None, []),

    ("emergency_contact", "Emergency Contact Number", FieldType.TEL, 1, True, OptionsSource.INLINE, False, False,
     "10-digit emergency contact", None, []),

    ("email_id", "Email ID", FieldType.EMAIL, 1, True, OptionsSource.INLINE, False, False,
     "candidate@example.com", None, []),

    ("aadhar_no", "Aadhar Number", FieldType.TEXT, 1, True, OptionsSource.INLINE, False, False,
     "12-digit Aadhar number", None, []),

    ("pan_number", "PAN Number", FieldType.TEXT, 1, True, OptionsSource.INLINE, False, False,
     "e.g. ABCDE1234F", "Format: 5 letters + 4 digits + 1 letter", []),

    ("designation", "Designation", FieldType.DROPDOWN, 1, True, OptionsSource.DESIGNATION, False, False,
     "Select designation", "Notice period will be auto-filled based on designation.", []),

    ("notice_period", "Notice Period", FieldType.TEXT, 1, False, OptionsSource.INLINE, False, True,
     "Auto-filled from designation", "Automatically calculated based on selected designation.", []),

    ("uan_number", "UAN Number", FieldType.TEXT, 1, False, OptionsSource.INLINE, False, False,
     "Universal Account Number (if applicable)", None, []),

    ("marital_status", "Marital Status", FieldType.DROPDOWN, 1, True, OptionsSource.INLINE, False, False,
     "Select marital status", None,
     [("Single", "Single"), ("Married", "Married"), ("Divorced", "Divorced"), ("Widowed", "Widowed")]),

    ("blood_group", "Blood Group", FieldType.DROPDOWN, 1, True, OptionsSource.INLINE, False, False,
     "Select blood group", None,
     [("A+", "A+"), ("A-", "A-"), ("B+", "B+"), ("B-", "B-"),
      ("AB+", "AB+"), ("AB-", "AB-"), ("O+", "O+"), ("O-", "O-")]),

    # ── Step 2: Employment Details ─────────────────────────────────────────────
    ("plant_location", "Plant Location", FieldType.DROPDOWN, 2, True, OptionsSource.PLANT_LOCATION, False, False,
     "Select plant location", None, []),

    ("contract_from", "Contract From (Date of Joining)", FieldType.DATE, 2, True, OptionsSource.INLINE, False, False,
     None, None, []),

    ("reporting_manager_name", "Reporting Manager Name", FieldType.TEXT, 2, True, OptionsSource.INLINE, False, False,
     "Manager's full name", None, []),

    ("reporting_manager_code", "Reporting Manager Employee Code", FieldType.TEXT, 2, True, OptionsSource.INLINE, False, False,
     "e.g. EMP-1234", None, []),

    ("salary_type", "Salary Type", FieldType.RADIO, 2, True, OptionsSource.INLINE, False, False,
     None, None,
     [("Net Salary", "Net Salary"), ("Net Stipend", "Net Stipend")]),

    ("salary_per_month", "Salary Per Month (₹)", FieldType.NUMBER, 2, True, OptionsSource.INLINE, False, False,
     "e.g. 25000", None, []),

    ("previous_organization", "Previous / Current Organization", FieldType.TEXT, 2, False, OptionsSource.INLINE, False, False,
     "Name of previous employer (if any)", None, []),

    ("current_salary", "Current Salary (In Hand, ₹)", FieldType.NUMBER, 2, False, OptionsSource.INLINE, False, False,
     "Current take-home salary", None, []),

    ("hiring_type", "Hiring Is", FieldType.RADIO, 2, True, OptionsSource.INLINE, False, False,
     None, None,
     [("New", "New"), ("Replacement", "Replacement")]),

    ("replacement_employee", "Replacement of Which Employee", FieldType.TEXT, 2, False, OptionsSource.INLINE, False, False,
     "Name of employee being replaced", "Required only if Hiring is Replacement.", []),

    ("replacement_employee_code", "Replacement Employee Code", FieldType.TEXT, 2, False, OptionsSource.INLINE, False, False,
     "Employee code of person being replaced", "Required only if Hiring is Replacement.", []),

    ("uniform_size", "Uniform Size", FieldType.DROPDOWN, 2, True, OptionsSource.INLINE, False, False,
     "Select size", None,
     [("S - 36", "S - 36"), ("M - 38", "M - 38"), ("L - 40", "L - 40"),
      ("XL - 42", "XL - 42"), ("XXL - 44", "XXL - 44")]),

    # ── Step 3: Bank & Documents ───────────────────────────────────────────────
    ("bank_name", "Bank Name", FieldType.TEXT, 3, True, OptionsSource.INLINE, False, False,
     "e.g. State Bank of India", None, []),

    ("account_number", "Account Number", FieldType.TEXT, 3, True, OptionsSource.INLINE, False, False,
     "Bank account number", None, []),

    ("ifsc_code", "IFSC Code", FieldType.TEXT, 3, True, OptionsSource.INLINE, False, False,
     "e.g. SBIN0001234", None, []),

    ("pan_card", "PAN Card (Photo/PDF)", FieldType.FILE, 3, True, OptionsSource.INLINE, False, False,
     None, "PDF or image, max 5MB", []),

    ("aadhar_card", "Aadhar Card (Photo/PDF)", FieldType.FILE, 3, True, OptionsSource.INLINE, False, False,
     None, "PDF or image, max 5MB", []),

    ("bank_details", "Bank Details (Statement/ Passbook Front/ Cancelled Cheque)", FieldType.FILE, 3, True, OptionsSource.INLINE, False, False,
     None, "Digital bank statement or passbook front page scan, max 5MB", []),

    ("passport_photo", "Passport Size Photo", FieldType.FILE, 3, True, OptionsSource.INLINE, False, False,
     None, "Recent passport size photograph (JPG/PNG)", []),
]


# ── Seed function ──────────────────────────────────────────────────────────────

with app.app_context():
    print("Dropping all tables...")
    db.drop_all()
    print("Creating all tables...")
    db.create_all()

    # Admin user
    admin = User(
        name="admin",
        email="admin@rdc.in",
        password_hash=bcrypt.generate_password_hash("Rdc@meow123456").decode("utf-8"),
        role=UserRole.SUPER_ADMIN,
    )
    db.session.add(admin)
    print("Created SUPER_ADMIN: admin@rdc.in / Rdc@meow123456")

    # Plant locations
    for i, name in enumerate(PLANTS, start=1):
        db.session.add(PlantLocation(name=name, sort_order=i))
    print(f"Seeded {len(PLANTS)} plant locations.")

    # Designations (default 30-day notice period)
    for i, name in enumerate(DESIGNATIONS, start=1):
        db.session.add(Designation(name=name, notice_period_days=30, sort_order=i))
    print(f"Seeded {len(DESIGNATIONS)} designations (notice period: 30 days — set per designation in admin).")

    # Form fields
    for sort_idx, (
        key, label, ftype, step, required, src, allow_other,
        readonly, placeholder, help_text, opts
    ) in enumerate(FORM_FIELDS, start=1):
        field = FormField(
            field_key=key,
            field_label=label,
            field_type=ftype,
            step=step,
            is_required=required,
            options_source=src,
            allow_other=allow_other,
            is_readonly=readonly,
            placeholder=placeholder,
            help_text=help_text,
            sort_order=sort_idx,
        )
        db.session.add(field)
        db.session.flush()
        for opt_i, (oval, olabel) in enumerate(opts):
            db.session.add(FormFieldOption(
                field_id=field.id,
                option_value=oval,
                option_label=olabel,
                sort_order=opt_i,
            ))
    print(f"Seeded {len(FORM_FIELDS)} form fields.")

    db.session.commit()
    print("\nSeed complete!")
    print("   Login: admin@rdc.in  /  Rdc@meow123456")
    print("   Change this password immediately after first login.")
