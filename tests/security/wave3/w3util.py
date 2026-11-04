"""Shared constants for the Wave 3 red-team tests."""
import w2stack as S

HI = dict(bcrypt_cost=4, terms_version=S.TERMS, cookie_secure=True, rl_register_ip=(10**6, 3600), rl_register_email=(10**6, 3600),
          rl_login_ip=(10**6, 300), rl_login_email=(10**6, 900), rl_resend_ip=(10**6, 3600), rl_resend_email=(10**6, 3600),
          rl_forgot_ip=(10**6, 3600), rl_forgot_email=(10**6, 3600), rl_reset_ip=(10**6, 900), rl_verify_ip=(10**6, 900),
          rl_change_password_user=(10**6, 900), rl_delete_user=(10**6, 900))
