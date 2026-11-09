from setuptools import setup

SKILL_ID = "ovos-skill-plex.mahdsip"

setup(
    name="ovos-skill-plex",
    version="0.1.0",
    description="OVOS Common Play skill to play music from a local Plex server",
    packages=["ovos_skill_plex"],
    package_data={"ovos_skill_plex": ["locale/*/*"]},
    include_package_data=True,
    install_requires=["ovos-workshop>=7.0.0", "ovos-utils>=0.1.0", "plexapi>=4.15"],
    entry_points={"opm.skill": f"{SKILL_ID}=ovos_skill_plex:PlexMusicSkill"},
)
