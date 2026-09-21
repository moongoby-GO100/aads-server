-- G3 fix: prevent_ops_skill_version_contract_mutation() was written for UPDATE
-- but the trigger fires on INSERT OR UPDATE.  On INSERT, OLD is NULL, so
-- "NEW.status IS DISTINCT FROM OLD.status" is always true and the allowed
-- transition list never matches — every new candidate version was rejected as
-- "skill version contract is immutable outside promotion lifecycle".  Nothing
-- could write a learned Site Skill.
--
-- The transition list was also out of step with the code that performs the
-- promotions (ohvis_harness.promote_skill_version / rollback_skill_version):
-- candidate->shadow, active->deprecated and deprecated->active were missing.
--
-- Shape mirrors trg_browser_learned_version_lifecycle, which already handles
-- INSERT correctly.  Additive and safe to apply repeatedly.
BEGIN;

CREATE OR REPLACE FUNCTION prevent_ops_skill_version_contract_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF NEW.manifest->>'status' IS DISTINCT FROM NEW.status THEN
        RAISE EXCEPTION 'skill version manifest status must match row status';
    END IF;
    IF NEW.content::jsonb IS DISTINCT FROM NEW.manifest THEN
        RAISE EXCEPTION 'skill version content must match manifest';
    END IF;
    IF NEW.content_sha256 IS DISTINCT FROM
       ('sha256:' || encode(digest(convert_to(NEW.content, 'UTF8'), 'sha256'), 'hex')) THEN
        RAISE EXCEPTION 'skill version content_sha256 must match content';
    END IF;

    IF TG_OP = 'INSERT' THEN
        -- A version may not be born active or retired; it has to earn that
        -- through the promotion lifecycle below.
        IF NEW.status IN ('active', 'deprecated', 'retired') THEN
            RAISE EXCEPTION 'skill versions may not be created in status %', NEW.status;
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.skill_id IS DISTINCT FROM OLD.skill_id
       OR NEW.version IS DISTINCT FROM OLD.version THEN
        RAISE EXCEPTION 'skill version identity is immutable';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF (OLD.status, NEW.status) NOT IN (
               ('candidate','shadow'), ('candidate','active'), ('candidate','quarantined'),
               ('shadow','active'), ('shadow','quarantined'),
               ('active','deprecated'), ('deprecated','active'), ('active','retired')
           ) THEN
            RAISE EXCEPTION 'skill version contract is immutable outside promotion lifecycle';
        END IF;
        IF NEW.manifest IS DISTINCT FROM jsonb_set(OLD.manifest, '{status}', to_jsonb(NEW.status)) THEN
            RAISE EXCEPTION 'skill version manifest may change only its status during promotion';
        END IF;
    ELSIF NEW.content IS DISTINCT FROM OLD.content
          OR NEW.manifest IS DISTINCT FROM OLD.manifest
          OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256 THEN
        RAISE EXCEPTION 'skill version contract is immutable outside promotion lifecycle';
    END IF;
    RETURN NEW;
END;
$fn$;

COMMIT;
