# Supabase PostgreSQL CA

Public certificate from the official Supabase CLI release v2.50.5:
https://github.com/supabase/cli/blob/v2.50.5/internal/gen/types/templates/prod-ca-2021.crt

Subject/issuer: Supabase Root 2021 CA. Valid until 2031-04-26.
SHA-256: 807025AD50D4ED219D2C9C7D299C004F824EB00CF7F65AFEF607D07B72E6CAFA

Used for Supabase database/pooler hosts with sslmode=verify-full. Explicit DSN sslrootcert overrides remain supported. This file contains a public CA certificate, not a private key or credential.
