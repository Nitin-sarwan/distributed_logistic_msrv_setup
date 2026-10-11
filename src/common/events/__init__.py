"""The event contracts, shared by every service.

This is the `libs/contracts/` the blueprint asks for, living inside `src/common`
because this repo is one deployable tree rather than a set of published
packages. The rule it exists to enforce is the same: **the envelope, the topic
name, and the event-type vocabulary are defined once**. A producer and a
consumer that each keep their own copy agree right up until one of them is
edited.

    envelope.py   the shape of every message
    types.py      every event type and the topic they travel on
    models.py     the outbox and processed-events tables, as mixins
    publisher.py  emitting an event == writing an outbox row
    relay.py      outbox row -> Kafka, the only thing that talks to a broker
    consumer.py   Kafka -> handler, with deduplication

Nothing here imports a service. Every service imports this.
"""
