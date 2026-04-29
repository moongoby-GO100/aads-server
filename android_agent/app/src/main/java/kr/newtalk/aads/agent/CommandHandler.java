package kr.newtalk.aads.agent;

import org.json.JSONObject;

interface CommandHandler {
    JSONObject handle(JSONObject params) throws Exception;
}
