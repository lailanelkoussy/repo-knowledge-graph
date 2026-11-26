"""
Messaging, application events and background work.

- Listeners: @KafkaListener (topics), @RabbitListener (queues), @JmsListener (destination),
  @SqsListener -> `channel` nodes (`channel:kafka:<topic>`, `channel:rabbitmq:<queue>`, ...) and
  `listens_to` edges (method -> channel).
- Producers: KafkaTemplate.send, RabbitTemplate.convertAndSend, JmsTemplate.convertAndSend,
  StreamBridge.send -> `publishes` edges, when the destination is a literal, a constant or a property.
- Application events: publishEvent(new OrderCreated(...)) -> `publishes` to `channel:event:<type>`,
  @EventListener / @TransactionalEventListener methods -> `listens_to` the event type of their parameter.
- Method attributes `spring.schedule` (@Scheduled), `spring.async` (@Async) and `spring.transactional`
  (@Transactional on the method or its class).
"""
import logging
from typing import Any, Dict, List, Optional

from ..java_extractor import resolve_constant_refs
from .base import (
    EnrichmentContext,
    annotation,
    annotation_names,
    annotation_value,
    as_list,
    enum_name,
    strip_class_literal,
)
from .spring_config import resolve_placeholders

logger = logging.getLogger(__name__)

LISTENERS = {
    'KafkaListener': ('kafka', ('topics', 'value', 'topicPattern')),
    'RabbitListener': ('rabbitmq', ('queues', 'value', 'queuesToDeclare')),
    'JmsListener': ('jms', ('destination', 'value')),
    'SqsListener': ('sqs', ('value', 'queueNames')),
    'StreamListener': ('stream', ('value', 'target')),
}
EVENT_LISTENERS = ('EventListener', 'TransactionalEventListener', 'ApplicationModuleListener')
PRODUCERS = {
    # receiver type suffix -> (broker, members, index of the destination argument)
    'KafkaTemplate': ('kafka', ('send', 'sendDefault'), 0),
    'KafkaProducerTemplate': ('kafka', ('send',), 0),
    'RabbitTemplate': ('rabbitmq', ('convertAndSend', 'send', 'convertSendAndReceive'), 0),
    'AmqpTemplate': ('rabbitmq', ('convertAndSend', 'send'), 0),
    'JmsTemplate': ('jms', ('convertAndSend', 'send'), 0),
    'JmsMessagingTemplate': ('jms', ('convertAndSend', 'send'), 0),
    'StreamBridge': ('stream', ('send',), 0),
    'SqsTemplate': ('sqs', ('send', 'convertAndSend'), 0),
}


def applies(context: EnrichmentContext) -> bool:
    wanted = set(LISTENERS) | set(EVENT_LISTENERS) | {'Scheduled', 'Async', 'Transactional', 'EnableScheduling'}
    for _, entity in context.java_declarations():
        attributes = entity.get('attributes') or {}
        if set(annotation_names(attributes)) & wanted:
            return True
        if any(i.get('member') == 'publishEvent' for i in attributes.get('invocations') or []):
            return True
    return False


class MessagingEnricher:
    def __init__(self, context: EnrichmentContext):
        self.context = context
        self.config = context.shared.get('config') or {}

    def text(self, value: Any) -> Optional[str]:
        if isinstance(value, dict):
            value = value.get('ref')
        if not isinstance(value, str) or not value:
            return None
        return resolve_placeholders(resolve_constant_refs(value, self.context.constants), self.config)

    def channel(self, broker: str, name: str, **attributes: Any) -> str:
        return self.context.add_resource('channel', f'{broker}:{name}', {
            'broker': broker, 'name': name, **{k: v for k, v in attributes.items() if v is not None}})

    def event_type(self, type_name: Optional[str], path: str) -> Optional[str]:
        if not type_name:
            return None
        return self.context.resolve(type_name, path) or type_name

    def run(self) -> None:
        context = self.context
        for path, entity in context.java_declarations():
            name = context.registry.lookup(entity.get('name', ''))
            if not name:
                continue
            attributes = entity.get('attributes') or {}
            if entity.get('type') in ('method', 'api_endpoint', 'constructor'):
                self.listeners(name, path, entity, attributes)
                self.tasks(name, path, entity, attributes)
            for invocation in attributes.get('invocations') or []:
                self.producer(name, path, invocation)

    def listeners(self, method: str, path: str, entity: dict, attributes: Dict[str, Any]) -> None:
        owner_attributes = (self.context.java_types.get(entity['name'].split('::', 1)[0]) or {}).get('attributes') or {}
        for listener, (broker, keys) in LISTENERS.items():
            item = annotation(attributes, listener)
            if item is None and annotation(attributes, 'KafkaHandler', 'RabbitHandler'):
                item = annotation(owner_attributes, listener)  # class-level listener + handler methods
            if not item:
                continue
            destinations: List[str] = []
            for key in keys:
                for value in as_list(annotation_value(item, key)):
                    if isinstance(value, dict) and 'annotation' in value:  # queuesToDeclare = @Queue("x")
                        value = annotation_value(value['annotation'], 'value', 'name')
                    resolved = self.text(value)
                    if resolved:
                        destinations.extend(d.strip() for d in resolved.split(',') if d.strip())
            group = self.text(annotation_value(item, 'groupId', 'id', 'containerFactory'))
            for destination in destinations:
                channel = self.channel(broker, destination, pattern=annotation_value(item, 'topicPattern') and True)
                self.context.add_edge(method, channel, 'listens_to', group=group)
            self.context.framework_attributes(method, 'spring')['listener'] = {
                'broker': broker, 'destinations': destinations, 'group': group}

        event_item = annotation(attributes, *EVENT_LISTENERS)
        if event_item:
            types = [strip_class_literal(v) for v in as_list(annotation_value(event_item, 'value', 'classes'))
                     if isinstance(v, str)]
            if not types:
                params = attributes.get('params') or []
                types = [params[0].get('type_resolved') or params[0].get('type')] if params else []
            for type_name in types:
                event = self.event_type(type_name, path)
                if event:
                    channel = self.channel('event', event, event_type=event)
                    self.context.add_edge(method, channel, 'listens_to',
                                          phase=annotation_value(event_item, 'phase'),
                                          condition=annotation_value(event_item, 'condition'))
            self.context.framework_attributes(method, 'spring')['event_listener'] = {
                'events': types, 'transactional': event_item.get('name') == 'TransactionalEventListener'}

    def producer(self, caller: str, path: str, invocation: Dict[str, Any]) -> None:
        member = invocation.get('member')
        args = invocation.get('args') or []
        receiver_type = (invocation.get('receiver_type') or '').rsplit('.', 1)[-1]
        receiver = (invocation.get('receiver') or '').rsplit('.', 1)[-1].lower()
        if member == 'publishEvent' and args:
            first = args[0]
            type_name = first.get('new') or first.get('type') if isinstance(first, dict) else None
            event = self.event_type(type_name, path)
            if event:
                channel = self.channel('event', event, event_type=event)
                self.context.add_edge(caller, channel, 'publishes', via='publishEvent')
            return
        for suffix, (broker, members, index) in PRODUCERS.items():
            matches_type = receiver_type.endswith(suffix)
            matches_name = not receiver_type and receiver.endswith(suffix.lower())
            if not (matches_type or matches_name) or member not in members:
                continue
            destination: Optional[str] = '(default topic)'
            if member != 'sendDefault':
                destination = self.text(args[index]) if len(args) > index else None
            if not destination:
                return
            if broker == 'rabbitmq' and member == 'convertAndSend' and len(args) >= 3:
                routing_key = self.text(args[1])
                channel = self.channel(broker, destination, exchange=destination, routing_key=routing_key)
            else:
                channel = self.channel(broker, destination)
            self.context.add_edge(caller, channel, 'publishes', via=f'{suffix}.{member}')
            return

    def tasks(self, method: str, path: str, entity: dict, attributes: Dict[str, Any]) -> None:
        context = self.context
        scheduled = annotation(attributes, 'Scheduled')
        if scheduled:
            schedule = {}
            for key, out in (('cron', 'cron'), ('fixedRate', 'fixed_rate'), ('fixedDelay', 'fixed_delay'),
                             ('fixedRateString', 'fixed_rate'), ('fixedDelayString', 'fixed_delay'),
                             ('initialDelay', 'initial_delay'), ('zone', 'zone')):
                value = annotation_value(scheduled, key)
                if value is not None:
                    schedule[out] = self.text(value) if isinstance(value, str) else value
            context.framework_attributes(method, 'spring')['schedule'] = schedule
        asynchronous = annotation(attributes, 'Async')
        if asynchronous:
            context.framework_attributes(method, 'spring')['async'] = {
                'executor': annotation_value(asynchronous, 'value')}
        owner = (context.java_types.get(entity['name'].split('::', 1)[0]) or {}).get('attributes') or {}
        transactional = annotation(attributes, 'Transactional')
        level = 'method'
        if not transactional and 'public' in (attributes.get('modifiers') or []):
            transactional, level = annotation(owner, 'Transactional'), 'class'
        if transactional:
            info: Dict[str, Any] = {'level': level}
            for key, out in (('propagation', 'propagation'), ('readOnly', 'read_only'),
                             ('isolation', 'isolation'), ('rollbackFor', 'rollback_for'), ('timeout', 'timeout')):
                value = annotation_value(transactional, key)
                if value is not None:
                    if isinstance(value, list):
                        info[out] = [strip_class_literal(v) if str(v).endswith('.class') else enum_name(v) for v in value]
                    elif key in ('propagation', 'isolation'):
                        info[out] = enum_name(value)
                    elif key == 'rollbackFor':
                        info[out] = [strip_class_literal(value)]
                    else:
                        info[out] = value
            context.framework_attributes(method, 'spring')['transactional'] = info


def enrich(context: EnrichmentContext) -> None:
    MessagingEnricher(context).run()
    channels: List[str] = [n for n, d in context.graph.nodes(data=True)
                           if getattr(d.get('data'), 'node_type', '') == 'channel']
    context.shared['channels'] = channels
