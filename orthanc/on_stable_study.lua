-- Orthanc Lua callback: fires when a study becomes "stable"
-- (no new DICOM instances received for StableAge seconds).
--
-- Automatically notifies the backend API to ingest the study
-- and route it to applicable AI use cases.
--
-- Logging uses print() — Orthanc's Lua has no PrintToLog(); calling it raised
-- "attempt to call a nil value" before HttpPost ran, so this webhook never fired.

function OnStableStudy(studyId, tags, metadata)
   local study_uid = tags["StudyInstanceUID"]
   if study_uid == nil or study_uid == "" then
      print("on_stable_study: StudyInstanceUID missing, skipping")
      return
   end

   local url = "http://backend:8000/api/orthanc/notify-stable-study"
   local body = '{"orthanc_id":"' .. studyId .. '","study_instance_uid":"' .. study_uid .. '"}'

   print("on_stable_study: notifying backend for study " .. study_uid)

   local ok, err = pcall(function()
      local headers = { ["Content-Type"] = "application/json" }
      -- Shared secret proving the call comes from this Orthanc (the backend rejects the
      -- webhook when its ORTHANC_WEBHOOK_SECRET is set and this header doesn't match).
      local secret = os.getenv("ORTHANC_WEBHOOK_SECRET")
      if secret ~= nil and secret ~= "" then
         headers["X-Orthanc-Webhook-Secret"] = secret
      end
      HttpPost(url, body, headers)
   end)

   if not ok then
      print("on_stable_study: failed to notify backend — " .. tostring(err))
   end
end
