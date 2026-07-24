UPDATE "scenes"
SET "visual" = CASE
  WHEN jsonb_typeof("visual"->'workerScene') = 'object'
    AND char_length("id") >= 1
    AND "scene_order" > 0
    AND char_length("script") >= 1
    AND jsonb_typeof("visual"->'workerScene'->'type') = 'string'
    AND "visual"->'workerScene'->>'type' IN ('avatar', 'image', 'image_video', 'stock_video', 'upload', 'chart', 'screenshot', 'text_fallback')
    AND jsonb_typeof("visual"->'workerScene'->'purpose') = 'string'
    AND char_length(btrim("visual"->'workerScene'->>'purpose')) BETWEEN 1 AND 500
    AND jsonb_typeof("visual"->'workerScene'->'durationEstimate') = 'number'
    AND ("visual"->'workerScene'->>'durationEstimate') ~ '^(0|[1-9][0-9]{0,8})([.][0-9]{1,6})?$'
    AND ("visual"->'workerScene'->>'durationEstimate')::numeric > 0
    AND ("visual"->'workerScene'->>'type' <> 'avatar' OR ("visual"->'workerScene'->>'durationEstimate')::numeric <= 12)
    AND jsonb_typeof("visual"->'layout') = 'string'
    AND char_length("visual"->>'layout') >= 1
    AND (
      NOT ("visual" ? 'headline')
      OR jsonb_typeof("visual"->'headline') IN ('string', 'null')
    )
    AND jsonb_typeof("visual"->'highlightWords') = 'array'
    AND NOT EXISTS (
      SELECT 1
      FROM jsonb_array_elements("visual"->'highlightWords') AS highlight(value)
      WHERE jsonb_typeof(highlight.value) <> 'string'
    )
    AND (
      NOT ("visual" ? 'visualPrompt')
      OR (
        jsonb_typeof("visual"->'visualPrompt') = 'string'
        AND char_length("visual"->>'visualPrompt') >= 1
      )
    )
    AND (
      "asset" IS NULL
      OR "asset" = 'null'::jsonb
      OR (
        jsonb_typeof("asset") = 'object'
        AND jsonb_typeof("asset"->'source') = 'string'
        AND (NOT ("asset" ? 'query') OR jsonb_typeof("asset"->'query') = 'string')
        AND (NOT ("asset" ? 'prompt') OR jsonb_typeof("asset"->'prompt') = 'string')
        AND (NOT ("asset" ? 'factual') OR jsonb_typeof("asset"->'factual') = 'boolean')
      )
    )
    AND (("visual"->>'contentRevision') IS NULL OR ("visual"->>'contentRevision') ~ '^(0|[1-9][0-9]{0,8})$')
  THEN ("visual" - 'workerScene') || jsonb_build_object(
    'sceneType', "visual"->'workerScene'->'type',
    'purpose', "visual"->'workerScene'->'purpose',
    'durationEstimate', "visual"->'workerScene'->'durationEstimate',
    'contentRevision', coalesce(("visual"->>'contentRevision')::integer, 0)
  )
  ELSE "visual" || jsonb_build_object(
    'contentRevision', 0,
    'canonicalSceneQuarantine', CASE
      WHEN jsonb_typeof("visual"->'workerScene') = 'object' THEN 'invalid_worker_scene_metadata'
      ELSE 'missing_canonical_scene_metadata'
    END
  )
END;
