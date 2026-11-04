-- Decision 012: LIMITED-SCOPE CI FIXTURE ONLY.
-- Source: schema/coin2win_baseline_schema.json at bdab36b0e4df580b02646f5b5faa186918b55db9
-- Not a production baseline restoration. Never apply to a persistent database.
CREATE ROLE coin2win NOLOGIN;
CREATE TABLE public.users (
  id varchar NOT NULL CONSTRAINT users_pkey PRIMARY KEY,
  role varchar NOT NULL DEFAULT 'player'
);
ALTER TABLE public.users OWNER TO coin2win;
CREATE TABLE public.brand_domains (
  id integer NOT NULL CONSTRAINT brand_domains_pkey PRIMARY KEY,
  domain varchar NOT NULL CONSTRAINT brand_domains_domain_key UNIQUE
);
CREATE TABLE public.softswiss_games (
  id integer NOT NULL CONSTRAINT softswiss_games_pkey PRIMARY KEY,
  provider_game_id text NOT NULL CONSTRAINT softswiss_games_provider_game_id_key UNIQUE
);
