from app import create_app
app = create_app()
with app.app_context():
    from app.models import OnboardingRequest, TrueinPushLog
    from app.integrations.truein import build_payload
    import json
    r = OnboardingRequest.query.filter_by(id=11).first()
    if r:
        p = build_payload(r)
        print('PAYLOAD:')
        print(json.dumps(p, indent=2))
        log = TrueinPushLog.query.filter_by(request_id=11).order_by(TrueinPushLog.attempted_at.desc()).first()
        if log:
            print('TRUEIN RESPONSE:', log.response_received[:300])
            print('PAYLOAD SENT:', log.payload_sent[:500])