"""Installable Sunday drama activity, using the host's shared APP sessions."""
from flask import Blueprint, jsonify, render_template, request

from features.checkin.client import CheckinError
from features.information.bills import BillError
from features.information.usage import UsageError
from features.information.services import ServicesError
from .client import DramaClient
from .scheduler import DramaScheduler, clean_message
from .video import DramaVideo
from .visits import TargetVisits


class Controller:
    def __init__(self, app, storage):
        self.deleted = False
        source = app.extensions['checkin_scheduler']
        def query(login_id, kind):
            try:
                if kind == 'usage':
                    return app.extensions['usage_queries'].query(login_id)
                bills = app.extensions['bill_queries']
                return bills.query(login_id, bills.months()[0])
            except (BillError, UsageError) as error:
                raise CheckinError(str(error), error.code, error.status) from error
        def client(login_id):
            provider = lambda: source.credentials(login_id)
            auth = source.client_factory(provider)
            target = TargetVisits(provider, auth.credential_cache, app.extensions['star_day_scheduler'])
            instance = DramaClient(provider, auth.credential_cache, auth=auth,
                query=lambda kind: query(login_id, kind),
                load_route=lambda: self.scheduler.data['route_tags'].get(login_id),
                save_route=lambda state: self.scheduler._change(lambda data: data['route_tags'].__setitem__(login_id, state)),
                play_short=lambda url, **kw: play_short(login_id, url, **kw),
                after_submit=lambda: self.scheduler.submitted(login_id), close_visits=target.close)
            def business(kind):
                if kind == 'v5': return target.v5()
                if kind == 'global_tone': return target.global_tone(instance)
                return query(login_id, kind)
            instance.query = business
            instance.visit_global = lambda:target.global_page(instance)
            return instance
        def video(login_id):
            provider = lambda: source.credentials(login_id)
            # Use the host's compatible CMLIVE cache even for an imported ZIP;
            # activity-owned observations and task leases stay in package storage.
            return DramaVideo(provider, source.client_factory(provider).credential_cache)
        def play_short(login_id, url, **kwargs):
            try:
                return video(login_id).complete_short(url, **kwargs,
                    stopped=lambda: self.scheduler.quit.is_set() or login_id in self.scheduler.cancelled)
            finally:
                self.scheduler.submitted(login_id)
        def gift_ready(login_id):
            provider = lambda:source.credentials(login_id)
            return TargetVisits(provider, source.client_factory(provider).credential_cache,
                                app.extensions['star_day_scheduler']).ready()
        def membership_reader(login_id):
            try:
                result = app.extensions['services_queries'].query(login_id)
            except ServicesError as error:
                raise CheckinError(str(error),error.code,error.status) from error
            matches=[]
            def collect(items):
                for item in items:
                    if any(name in item['name'] for name in ('爱奇艺','随心看')):
                        matches.append({key:item.get(key) for key in ('name','fee','status')})
                    collect(item.get('children',[]))
            for group in result['groups']:
                collect(group['items'])
            return matches
        self.scheduler = DramaScheduler(source.accounts, client, video, storage, gift_ready=gift_ready,
                                        membership_reader=membership_reader)

    def metadata(self):
        return self.scheduler.metadata()

    def busy_login(self, login_id):
        return self.scheduler.busy_login(login_id)

    def forget(self, login_id):
        self.scheduler.forget(login_id)

    def stop(self, wait=True):
        self.scheduler.shutdown(wait=wait)


def register(app, credentials, accounts, storage):
    controller = Controller(app, storage)
    blueprint = Blueprint('sunday_drama', __name__, template_folder='templates', static_folder='static',
                          static_url_path='/activities/sunday-drama/static')

    @blueprint.before_request
    def available():
        if controller.deleted:
            raise CheckinError('该活动已移除。', 'PLATFORM_DELETED', 410)

    @blueprint.errorhandler(CheckinError)
    def error_handler(error):
        return jsonify(error=clean_message(error), code=error.code), error.status

    @blueprint.get('/sunday-drama')
    def page():
        return render_template('sunday_drama.html')

    @blueprint.get('/api/sunday-drama/status')
    def status():
        return jsonify(controller.scheduler.status())

    @blueprint.post('/api/sunday-drama/run')
    def run():
        payload = request.get_json(silent=True)
        if (not isinstance(payload, dict) or payload.get('kind') not in
                {'refresh', 'tasks', 'task', 'video', 'draw', 'invite_claim'}
                or payload.get('id') is not None and not isinstance(payload['id'], str)
                or payload['kind'] in {'refresh', 'task', 'video', 'draw', 'invite_claim'} and not payload.get('id')
                or payload['kind'] == 'task' and not isinstance(payload.get('task_id'), str)
                or payload['kind'] == 'invite_claim' and (not isinstance(payload.get('prize_id'), str)
                                                        or payload.get('confirmed') is not True)):
            raise CheckinError('请选择账号、操作及需要确认的奖励。', 'INVALID_OPERATION', 400)
        return jsonify(controller.scheduler.queue(payload.get('id'), payload['kind'],
            task_id=payload.get('task_id'), prize_id=payload.get('prize_id')))

    @blueprint.post('/api/sunday-drama/settings')
    def settings():
        return jsonify(controller.scheduler.settings(request.get_json(silent=True)))

    @blueprint.post('/api/sunday-drama/invite')
    def invitation():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get('id'), str):
            raise CheckinError('请选择共享账号。', 'INVALID_ACCOUNT', 400)
        return jsonify(controller.scheduler.invitation(payload['id']))

    @blueprint.post('/api/sunday-drama/membership')
    def membership():
        payload=request.get_json(silent=True)
        if not isinstance(payload,dict) or not isinstance(payload.get('id'),str):
            raise CheckinError('请选择共享账号。','INVALID_ACCOUNT',400)
        return jsonify(controller.scheduler.membership(payload['id']))

    app.register_blueprint(blueprint)
    return controller
