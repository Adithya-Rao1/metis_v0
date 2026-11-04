# 0001 Baseline schema
Forced RLS on all tenant tables; pre-auth via SECURITY DEFINER functions owned by a minimal BYPASSRLS role; audit and terms append-only by trigger and privilege; model publish gate as a CHECK. Alternatives: app-only authorization (rejected: single layer), Postgres schema-per-tenant (rejected: operational cost).
