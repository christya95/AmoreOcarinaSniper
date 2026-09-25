# AmoreOcarinaSniper

Project brief for a Python Amazon.ca purchase coordinator and a companion browser extension that detects matching stock alerts rendered in an open Discord channel.

**Status:** Planning only. No extension or purchasing application has been implemented or tested.

## Start in Cursor

Open this repository in Cursor and give the agent [CURSOR_PROMPT.md](CURSOR_PROMPT.md). It contains the implementation requirements, safety controls, and acceptance tests.

Target: Amazon.ca ASIN `B0HJ6F8L6V`, described in the supplied notification as Nintendo Switch 2 – The Legend of Zelda – 40th Anniversary Edition. The listing and current offer still need verification. The example CAD 709.99 alert is not a spending authorization.

The source Discord server prohibits bots except those operated by moderators. Whether local DOM monitoring is permitted remains unresolved; this design does not establish an exemption.

Never commit credentials, browser profiles, session state, payment information, or private checkout artifacts.
