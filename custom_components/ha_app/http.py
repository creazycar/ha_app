import time, json, aiohttp, logging
from homeassistant.components.http import HomeAssistantView

from homeassistant.util.json import load_json
from homeassistant.helpers.network import get_url
from homeassistant.const import __version__ as current_version

from .manifest import manifest
from .utils import call_service, async_http_post, async_register_sensor, \
    get_storage_dir, timestamp_state, md5, get_notifications
from .const import CONVERSATION_ASSISTANT
_LOGGER = logging.getLogger(__name__)

class HttpView(HomeAssistantView):

    url = "/api/haapp"
    name = "api:haapp"
    requires_auth = False
    # 计数器
    count = 0
    # 设备名称
    device = {}

    def get_device(self, webhook_id):
        ''' 获取设备信息 '''
        if webhook_id not in self.device:
            config_entries = load_json(get_storage_dir('core.config_entries'))
            entries = config_entries['data']['entries']
            for entity in entries:
                entity_data = entity.get('data')
                if entity_data is not None and entity_data.get('webhook_id') == webhook_id:
                    self.device[webhook_id] = {
                        'id': entity_data.get('device_id'),
                        'latitude': 0,
                        'longitude': 0
                    }
        return self.device.get(webhook_id)


    async def post(self, request):
        ''' 保留通知消息 '''
        hass = request.app["hass"]

        body = await request.json()
        _LOGGER.debug(body)
        
        push_token = body.get('push_token')
        title = body.get('title')
        message = body.get('message')

        registration_info = body.get('registration_info')
        webhook_id = registration_info.get('webhook_id')

        result = {
            'title': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()) if title is None else title,
            'message': message,
        }

        # 附加数据
        data = body.get('data')
        if data is not None:
            # 链接
            url = data.get('url')
            if url is not None:
                result['url'] = url
            # TTS
            tts = data.get('tts')
            if tts is not None:
                result['tts'] = tts
            # 启动项
            start = data.get('start')
            if start is not None:
                result['start'] = start

        device = self.get_device(webhook_id)
        notification_id = f'{md5(device.get("id"))}{time.strftime("%m%d%H%M%S", time.localtime())}{self.count}'
        self.count = self.count + 1
        if self.count > 50:
            self.count = 0

        call_service(hass, 'persistent_notification.create', {
                    'title': message,
                    'message': json.dumps(result, indent=2, ensure_ascii=False),
                    'notification_id': notification_id
                })
        return self.json_message("推送成功", status_code=201)

    async def put(self, request):
        ''' 上传GPS信息 '''
        result = await self.async_validate_access_token(request)
        if result is not None:
            return result

        # 上报GPS位置        
        hass = request.app["hass"]
        body = await request.json()
        _LOGGER.debug(body)
        
        webhook_id = body.get('webhook_id')
        if webhook_id is None:
            return self.json([])

        # 获取设备webhook地址
        base_url = get_url(hass)
        webhook_url = f"{base_url}/api/webhook/{webhook_id}"

        _type = body.get('type')
        data = body.get('data')

        # 发送事件
        if ['notify', 'sms', 'button'].count(_type) > 0:
            hass.bus.fire('ha_app', { 'type': _type, 'data': data })

        device = self.get_device(webhook_id)
        if _type == 'gps': # 位置
            hass.loop.create_task(self.async_update_device(hass, webhook_url, data, device))
        elif _type == 'notify': # 通知                
            hass.loop.create_task(self.async_update_notify(hass, webhook_url, data))
        elif _type == 'notify_list': # 通知列表
            for item in data:
                await self.async_update_notify(hass, webhook_url, item)
        elif _type == 'sms': # 短信
            hass.loop.create_task(self.async_update_sms(hass, webhook_url, data))
        elif _type == 'nfc': # NFC
            hass.loop.create_task(self.async_update_nfc(hass, webhook_url, data))
        elif _type == 'event': # 系统事件
            hass.loop.create_task(self.async_update_event(hass, webhook_url, data))

        # 使用新版通知
        notifications = get_notifications(hass, device.get('id'))

        response = {
            'notify': notifications
        }
        if _type == 'conversation.list':
            ''' 对话记录 '''
            headers = {
                'Authorization': request.headers.get('Authorization')
            }
            result = []
            async with aiohttp.ClientSession() as session:
                async with session.get(f'{base_url}/api/history/period/2023-07-26T00:00:00.000Z', params={
                    'filter_entity_id': 'conversation.voice'
                }, headers=headers) as response:
                    arr = await response.json()
                    if len(arr) > 0:
                        def format_item(state):
                            attrs = state['attributes']
                            return {
                                'command': state['state'],
                                'reply': attrs.get('reply')
                            }
                        result = map(format_item, arr[0])
            response['result'] = result
        elif _type == 'conversation.process':
            ''' 控制命令 '''
            conversation = self.hass.data.get(CONVERSATION_ASSISTANT)
            if conversation is not None:
                res = await conversation.recognize(data)
                response['result'] = res.response
        elif _type == 'ha.config':
            ''' 基本配置 '''
            response['result'] = {
                'internal_url': get_url(hass),
                'external_url': get_url(hass, prefer_external=True),
                'ha_version': current_version,
                'ha_app_version': manifest.version
            }
        return self.json(response)

    async def delete(self, request):
        ''' 清除通知 '''
        result = await self.async_validate_access_token(request)
        if result is not None:
            return result

        hass = request.app["hass"]
        query = request.query
        ids = query.get('id').split(',')
        for notification_id in ids:
            call_service(hass, 'persistent_notification.dismiss', { 'notification_id': notification_id })
        return self.json_message("删除通知提醒", status_code=200)

    def get_access_token(self, request):
        authorization = request.headers.get('Authorization')
        return str(authorization).replace('Bearer', '').strip()

    async def async_validate_access_token(self, request):
        ''' 授权验证 '''
        hass = request.app["hass"]
        hass_access_token = self.get_access_token(request)
        # print(hass_access_token)
        token = await hass.auth.async_validate_access_token(hass_access_token)
        if token is None:
            return self.json_message("未授权", status_code=401)

    async def async_update_device(self, hass, webhook_url, body, device):
        ''' 更新设备 '''
        latitude = body.get('latitude')
        longitude = body.get('longitude')
        battery = body.get('battery')
        gps_accuracy = body.get('gps_accuracy')

        # 更新位置
        result = await async_http_post(webhook_url, {
            "type": "update_location",
            "data": {
                "gps": [latitude, longitude],
                "gps_accuracy": gps_accuracy,
                "battery": battery
            }
        })
        if result is not None:
            await self.async_update_battery(hass, webhook_url, battery)
            # 鹰眼轨迹服务
            map = hass.data.get(manifest.domain)
            if map is not None:
                device_id = device.get('id')
                if device.get('latitude') == latitude and device.get('longitude') == longitude:
                    _LOGGER.debug('位置相同不上报，节省额度')
                else:
                    await map.async_add_point(device_id, latitude, longitude, gps_accuracy)
                # 记录坐标
                device['latitude'] = latitude
                device['longitude'] = longitude

    async def async_update_battery(self, hass, webhook_url, battery):
        ''' 更新电量 '''
        battery = int(battery)
        icon = "mdi:battery"

        if battery <= 90:
            icon = "mdi:battery-90"
        elif battery <= 80:
            icon = "mdi:battery-80"
        elif battery <= 70:
            icon = "mdi:battery-70"
        elif battery <= 60:
            icon = "mdi:battery-60"
        elif battery <= 50:
            icon = "mdi:battery-50"
        elif battery <= 40:
            icon = "mdi:battery-40"
        elif battery <= 30:
            icon = "mdi:battery-30"
        elif battery <= 20:
            icon = "mdi:battery-20"
        elif battery <= 10:
            icon = "mdi:battery-10"
        
        await async_register_sensor(webhook_url, 
                unique_id="battery_level", 
                icon=icon,
                state=battery,
                attributes={},
                register_data={
                    "state_class": "measurement",
                    "device_class": "battery",
                    "unit_of_measurement": "%",
                    "name": "电量",
                }
            )

    async def async_update_sms(self, hass, webhook_url, data):
        ''' 更新短信 '''
        await async_register_sensor(webhook_url, 
                unique_id="short_message", 
                icon="mdi:message",
                state=timestamp_state(hass),
                attributes={
                    "from": str(data['from']),
                    "text": data['content']
                },
                register_data={
                    "name": "短信",
                    "device_class": "timestamp"
                }
            )

    async def async_update_nfc(self, hass, webhook_url, data):
        ''' 更新NFC '''
        await async_register_sensor(webhook_url, 
                unique_id="scan_nfc", 
                icon="mdi:nfc-variant",
                state=timestamp_state(hass),
                attributes={
                    "id": data['id']
                },
                register_data={
                    "name": "NFC",
                    "device_class": "timestamp"
                }
            )

    async def async_update_notify(self, hass, webhook_url, data):
        ''' 更新通知 '''
        await async_register_sensor(webhook_url, 
                unique_id="application_notification", 
                icon="mdi:cellphone-message",
                state=timestamp_state(hass),
                attributes={
                    "title": data['title'],
                    "content": data['content'],
                    "text": data['text'],
                    "package": data['package']
                },
                register_data={
                    "device_class": "timestamp",
                    "name": "通知",
                }
            )

    async def async_update_event(self, hass, webhook_url, data):
        ''' 系统事件 '''
        battery = data.get('battery')
        state = data.get('text')

        await async_register_sensor(webhook_url, 
                unique_id="system_event", 
                icon="mdi:cellphone-information",
                state=state,
                attributes={},
                register_data={
                    "device_class": "timestamp",
                    "name": "系统事件",
                }
            )
        # 更新手机电量
        await self.async_update_battery(hass, webhook_url, battery)
