-- Orthanc Lua callback: fires when a study becomes "stable"
-- (no new DICOM instances received for StableAge seconds).
--
-- Automatically notifies the backend API to ingest the study
-- and route it to applicable AI use cases.

function OnStableStudy(studyId, tags, metadata)
   local study_uid = tags["StudyInstanceUID"]
   if study_uid == nil or study_uid == "" then
      PrintToLog("on_stable_study: StudyInstanceUID missing, skipping", "WARNING")
      return
   end

   local url = "http://backend:8000/api/orthanc/notify-stable-study"
   local body = '{"orthanc_id":"' .. studyId .. '","study_instance_uid":"' .. study_uid .. '"}'

   PrintToLog("on_stable_study: notifying backend for study " .. study_uid, "INFO")

   local ok, err = pcall(function()
      local headers = { ["Content-Type"] = "application/json" }
      HttpPost(url, body, headers)
   end)

   if not ok then
      PrintToLog("on_stable_study: failed to notify backend — " .. tostring(err), "ERROR")
   end
end
