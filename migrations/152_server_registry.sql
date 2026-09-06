-- 서버 정보 동적 레지스트리 테이블
CREATE TABLE IF NOT EXISTS server_registry (
    server_key TEXT PRIMARY KEY,
    ip TEXT NOT NULL,
    port INTEGER DEFAULT 22,
    workdir TEXT NOT NULL,
    project TEXT NOT NULL,
    ssh_user TEXT DEFAULT 'root',
    description TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO server_registry (server_key, ip, port, workdir, project, ssh_user, description) VALUES
  ('contabo116', '5.104.86.116', 22, '/root/aads/aads-server', 'AADS', 'root', 'AADS Backend+Dashboard+PostgreSQL'),
  ('contabo14', '5.104.86.14', 22, '/root/kis-autotrade-v4', 'KIS', 'root', 'KIS/GO100 실행 환경'),
  ('cafe24_114', '114.207.244.86', 7916, '/data/shortflow', 'SF', 'root', 'SF/NTV2/NAS 실행 환경')
ON CONFLICT (server_key) DO NOTHING;
