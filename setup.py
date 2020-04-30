from setuptools import setup, find_packages

setup(
    name="vxturn",
    version="0.0.1",
    url='http://github.com/praekelt/vumi-turn',
    license='BSD',
    description="A Vumi WhatsApp transport for Turn.",
    long_description=open('README.md', 'r').read(),
    author='Praekelt Foundation',
    author_email='dev@praekeltfoundation.org',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'cryptography==1.5.1',
        'vumi==0.6.18',
        'treq',
    ],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: POSIX',
        'Programming Language :: Python',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Topic :: System :: Networking',
    ],
)
