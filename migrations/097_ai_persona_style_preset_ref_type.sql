-- Allow generated style preset images to be linked as persona references.
-- Previous style preset jobs produced images but failed during reference insert
-- because the ai_persona_references ref_type check did not include this stage.

ALTER TABLE ai_persona_references
DROP CONSTRAINT IF EXISTS ai_persona_references_ref_type_check;

ALTER TABLE ai_persona_references
ADD CONSTRAINT ai_persona_references_ref_type_check
CHECK (
    ref_type IN (
        'face_seed',
        'face_front',
        'face_quarter_left',
        'face_quarter_right',
        'face_side_left',
        'face_side_right',
        'face_tilt_up',
        'face_tilt_down',
        'fullbody_stand',
        'fullbody_walk',
        'fullbody_sit',
        'fullbody_lean',
        'fullbody_turn',
        'fullbody_other',
        'style_preset'
    )
);
