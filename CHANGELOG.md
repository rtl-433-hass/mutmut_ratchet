# Changelog

## [0.4.0](https://github.com/rtl-433-hass/mutmut_ratchet/compare/v0.3.0...v0.4.0) (2026-09-13)


### Features

* **shards:** bin-pack functions, not just modules ([#14](https://github.com/rtl-433-hass/mutmut_ratchet/issues/14)) ([47f94f6](https://github.com/rtl-433-hass/mutmut_ratchet/commit/47f94f66f6ca8ac3f29c089c44e71e500cc91f3d))

## [0.3.0](https://github.com/rtl-433-hass/mutmut_ratchet/compare/v0.2.0...v0.3.0) (2026-09-13)


### Features

* **timings:** record per-function seconds, not just per-file ([5f45187](https://github.com/rtl-433-hass/mutmut_ratchet/commit/5f45187d40bcb28d76233e0503439aba5ce34339))

## [0.2.0](https://github.com/rtl-433-hass/mutmut_ratchet/compare/v0.1.0...v0.2.0) (2026-09-13)


### Features

* **ratchet:** gate a function-scoped run on new survivors ([9173ea7](https://github.com/rtl-433-hass/mutmut_ratchet/commit/9173ea7a6a6abb45e32dba6037551228c3891ec0))
* **shards:** let a shard narrow its patterns to named functions ([be04ab7](https://github.com/rtl-433-hass/mutmut_ratchet/commit/be04ab73aacba8ff659de2f902e4e52fb38e686e))
* **stats:** tally mutants per function, not just per file ([78ce8e9](https://github.com/rtl-433-hass/mutmut_ratchet/commit/78ce8e99ed10750eb08385cb1b97da4e67658cb5))
* **targets:** scope a PR's mutation run to its changed functions ([1d3b4f9](https://github.com/rtl-433-hass/mutmut_ratchet/commit/1d3b4f94bae74c360c91a71ecb2122b63f4c7881))

## 0.1.0 (2026-09-02)


### Features

* **cli:** add the mutmut-ratchet console script ([c4c8c4d](https://github.com/rtl-433-hass/mutmut_ratchet/commit/c4c8c4decf668f75b9abb6f4af3bed7557c1bd6f))
* **config:** read tooling settings from [tool.mutmut_ratchet] ([6497922](https://github.com/rtl-433-hass/mutmut_ratchet/commit/6497922fa039e55e111cdf8b19921724009ee3f5))
* **ratchet:** enforce the per-file mutation-score floor ([b3b2ce8](https://github.com/rtl-433-hass/mutmut_ratchet/commit/b3b2ce856710a87657e43cdaf4e0967b2d9ec328))
* **shards:** partition modules into time-balanced shards ([071ebe8](https://github.com/rtl-433-hass/mutmut_ratchet/commit/071ebe8fab1751356c759de487ec3bcf042eae99))
* **stats:** export per-file mutmut statistics as JSON ([6c1f73b](https://github.com/rtl-433-hass/mutmut_ratchet/commit/6c1f73bb52c4cda6b37ed322775b5ef492542df4))
* **targets:** map a PR's changed files to mutation targets ([86c5319](https://github.com/rtl-433-hass/mutmut_ratchet/commit/86c5319a061394d75ac86d4c5ded5b9bd59874d6))
* **timings:** record the per-file mutmut runtime profile ([17ae7cb](https://github.com/rtl-433-hass/mutmut_ratchet/commit/17ae7cbfac5e37e816384ffd1e1f0fc701e59cdf))


### Bug Fixes

* **ci:** correct the release-please config and bootstrap 0.1.0 ([#4](https://github.com/rtl-433-hass/mutmut_ratchet/issues/4)) ([fdd9814](https://github.com/rtl-433-hass/mutmut_ratchet/commit/fdd9814a4da26905f7f427d33a87db11301bdb62))
* **targets:** match a package __init__'s real mutant names ([9033325](https://github.com/rtl-433-hass/mutmut_ratchet/commit/903332514ca87d3f311739c6eacad630145a10b1))


### Documentation

* document the configuration and a worked CI example ([d690684](https://github.com/rtl-433-hass/mutmut_ratchet/commit/d690684dc5387a2d23a2ae16bc942e0a9eedf6ec))

## Changelog

All notable changes to this project are documented here. The format follows
[Conventional Commits](https://www.conventionalcommits.org/) and releases are
cut by [release-please](https://github.com/googleapis/release-please).
