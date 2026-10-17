"""Agents. They share the operation graph and coordinate only through it.

- triage: claims new disruptions, analyses them, hands them to a desk.
- notifier: marks the NOTIFIES edges as delivered.
- assistant: answers questions for one user, with that user's memory graph."""
