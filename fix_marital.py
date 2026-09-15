from app import create_app
app = create_app()
with app.app_context():
    from app.models import FormField, FormFieldOption
    from app.extensions import db

    # Find the marital_status field
    field = FormField.query.filter_by(field_key='marital_status', is_deleted=False).first()
    if not field:
        print('marital_status field not found')
    else:
        print(f'Found field: {field.field_label} (id={field.id})')
        existing = FormFieldOption.query.filter_by(field_id=field.id).all()
        print('Current options:', [o.option_value for o in existing])

        # Replace with only Truein-valid options
        FormFieldOption.query.filter_by(field_id=field.id).delete()

        valid_options = [
            ('Married',   'Married',   0),
            ('Unmarried', 'Unmarried', 1),
            ('Other',     'Other',     2),  # maps to blank in Truein (not sent)
        ]
        for label, value, order in valid_options:
            db.session.add(FormFieldOption(
                field_id=field.id,
                option_label=label,
                option_value=value,
                sort_order=order,
            ))
        db.session.commit()
        print('Updated options:', [o.option_value for o in FormFieldOption.query.filter_by(field_id=field.id).all()])