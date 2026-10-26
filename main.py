from __future__ import annotations
import json, re, time
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.message_components import Face
from .engine import Store, build_context, DEFAULT_GUIDANCE


@register("astrbot_plugin_companion_generic", "Open Companion", "可配置的私聊角色连续性与偏好插件", "1.1.0")
class GenericCompanion(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context); self.config = config
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_companion_generic")
        seed = self._json_config("initial_preferences_json", {})
        self.store = Store(self.data_dir / "state.sqlite3", seed if isinstance(seed, dict) else {})

    def _json_config(self, key, default):
        value = self.config.get(key, default)
        if isinstance(value, (dict, list)): return value
        try: return json.loads(value) if value else default
        except Exception: return default

    def _list_config(self, key):
        value = self.config.get(key, [])
        if isinstance(value, list): return [str(x).strip() for x in value if str(x).strip()]
        return [x.strip() for x in str(value).splitlines() if x.strip()]

    def scope(self, event):
        if not self.config.get("enabled", True) or not event.is_private_chat(): return None
        owner = str(self.config.get("owner_qq", "")).strip(); bot = str(self.config.get("bot_qq", "")).strip()
        if not owner or not bot or str(event.get_sender_id()) != owner or str(event.get_self_id()) != bot: return None
        platform = str(self.config.get("platform_name", "aiocqhttp")).strip()
        if platform and event.get_platform_name() != platform: return None
        instance = str(self.config.get("platform_id", "")).strip()
        if instance and event.get_platform_id() != instance: return None
        return json.dumps([event.get_platform_id(), event.get_self_id(), event.get_sender_id()], ensure_ascii=False)

    @staticmethod
    def proactive(event):
        return str(getattr(event.message_obj, "message_id", "")).startswith("proactive:") and hasattr(event, "_proactive_plugin")

    def settings(self):
        mood_text = self._json_config("mood_text_json", {})
        if not isinstance(mood_text, dict): mood_text = {}
        return {"mood_text": mood_text}

    def half_life(self): return max(5, min(240, int(self.config.get("mood_half_life_minutes", 45))))

    def phrase_map(self):
        raw = self._json_config("mood_trigger_json", {})
        return raw if isinstance(raw, dict) else {}

    def activity_map(self):
        raw = self._json_config("activity_rules_json", {})
        return raw if isinstance(raw, dict) else {}

    def guidance(self):
        text = str(self.config.get("guidance_text", "")).strip()
        return text or None

    @filter.on_llm_request(priority=-100)
    async def prepare(self, event: AstrMessageEvent, req: ProviderRequest):
        scope = self.scope(event)
        if not scope or event.get_extra("companion_prepared"): return
        state = self.store.view(scope, self.half_life()); proactive = self.proactive(event)
        if proactive and self.config.get("quiet_guard", True) and state["quiet_until"] > time.time():
            logger.info("[generic companion] 安静时段，跳过本次主动生成。"); event.stop_event(); return
        if not proactive:
            text = str(event.message_str or "")
            if text.lstrip('/ ').startswith('伴侣'): return
            if self.config.get('auto_memory_enabled', False):
                categories = self._list_config('auto_memory_categories') or ['称呼', '喜好', '表达']
                self.store.auto_remember(scope, text,
                    max(1, min(100, int(self.config.get('max_preferences', 24)))), categories,
                    max(1, min(3, int(self.config.get('auto_memory_max_per_message', 2)))))
            state = self.store.observe(scope, text, self.half_life(), self.config.get("auto_activity", True), self.phrase_map(), self.activity_map())
        faces = [m.id for m in event.get_messages() if isinstance(m, Face)]
        req.system_prompt = (req.system_prompt or "") + "\n\n" + build_context(state, faces, DEFAULT_GUIDANCE + '\n' + (self.guidance() or ''), self.settings())
        event.set_extra("companion_prepared", True)

    @filter.on_llm_response(priority=-100)
    async def after_response(self, event: AstrMessageEvent, response: LLMResponse):
        scope = self.scope(event)
        if not scope or not event.get_extra("companion_prepared") or getattr(response, "is_chunk", False) or getattr(response, "tools_call_name", None): return
        if getattr(response, "role", "assistant") == "assistant": self.store.response(scope, getattr(response, "completion_text", "") or "", max(1, min(10, int(self.config.get("recent_reply_count", 3)))), max(40, min(1000, int(self.config.get("recent_reply_chars", 240)))))

    def command_ok(self, event): return self.scope(event)
    def prefix(self): return "/伴侣"

    @filter.command("伴侣帮助")
    async def help_command(self, event: AstrMessageEvent):
        if not self.command_ok(event): return
        event.stop_event(); p=self.prefix()
        yield event.plain_result(f"{p}状态\n{p}记忆\n{p}记住 偏好名=具体内容\n{p}忘记 偏好名\n{p}安静 90\n{p}恢复\n{p}重置 确认\n只管理本插件的私聊状态。")

    @filter.command("伴侣状态")
    async def status_command(self, event: AstrMessageEvent):
        scope=self.command_ok(event)
        if not scope:return
        s=self.store.view(scope,self.half_life()); left=max(0,int((s["quiet_until"]-time.time())/60)); event.stop_event()
        yield event.plain_result(f"{self.config.get('bot_name','角色')} 专属插件运行中\n角色心情：{s['mood']}\n来源：{s['reason'] or '自然淡化后的平常状态'}\n偏好：{len(s['preferences'])} 条\n安静时段：剩余约 {left} 分钟")

    @filter.command("伴侣记忆")
    async def list_command(self, event: AstrMessageEvent):
        scope=self.command_ok(event)
        if not scope:return
        s=self.store.view(scope); event.stop_event()
        labels = {'auto': '自动', 'manual': '手动', 'initial': '初始'}
        yield event.plain_result("本插件保存的偏好：\n"+("\n".join(f"{k}：{v} [{labels.get(s['preference_meta'].get(k, {}).get('source'), '手动')}]" for k,v in s["preferences"].items()) or "暂无"))

    def arg(self,event):
        parts=event.message_str.split(maxsplit=1); return parts[1].strip() if len(parts)>1 else ""

    @filter.command("伴侣记住")
    async def remember_command(self,event:AstrMessageEvent):
        scope=self.command_ok(event)
        if not scope:return
        event.stop_event(); text=self.arg(event)
        if "=" not in text: yield event.plain_result(f"用法：{self.prefix()}记住 偏好名=具体内容"); return
        key,value=text.split("=",1)
        try:self.store.remember(scope,key,value,max(1,min(100,int(self.config.get("max_preferences",24))))); reply="已保存，可用记忆命令查看。"
        except ValueError as e:reply=str(e)
        yield event.plain_result(reply)

    @filter.command("伴侣忘记")
    async def forget_command(self,event:AstrMessageEvent):
        scope=self.command_ok(event)
        if not scope:return
        event.stop_event(); yield event.plain_result("已删除。" if self.store.forget(scope,self.arg(event)) else "没找到这条偏好。")

    @filter.command("伴侣安静")
    async def quiet_command(self,event:AstrMessageEvent,minutes:int=90):
        scope=self.command_ok(event)
        if not scope:return
        event.stop_event()
        if not 1<=minutes<=1440:yield event.plain_result("请填写 1—1440 分钟。");return
        self.store.quiet(scope,minutes);yield event.plain_result(f"已安静 {minutes} 分钟。")

    @filter.command("伴侣恢复")
    async def resume_command(self,event:AstrMessageEvent):
        scope=self.command_ok(event)
        if not scope:return
        event.stop_event();self.store.quiet(scope,0);yield event.plain_result("安静时段已解除。")

    @filter.command("伴侣重置")
    async def reset_command(self,event:AstrMessageEvent,confirmation:str=""):
        scope=self.command_ok(event)
        if not scope:return
        event.stop_event()
        if confirmation!="确认":yield event.plain_result(f"确定请发：{self.prefix()}重置 确认");return
        self.store.reset(scope);yield event.plain_result("本插件的状态和偏好已重置。")

    async def terminate(self): self.store.close()
