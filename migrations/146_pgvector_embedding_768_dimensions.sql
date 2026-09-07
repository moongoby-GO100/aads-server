-- Align all AADS pgvector embedding columns with local nomic-embed-text.
-- nomic-embed-text returns 768-dimensional vectors.

ALTER TABLE experience_memory
    ALTER COLUMN embedding TYPE vector(768);

ALTER TABLE project_memory
    ALTER COLUMN embedding TYPE vector(768);
