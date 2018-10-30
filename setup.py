import setuptools


setuptools.setup(
    version="0.0.1",
    license='mit',
    name="py-schema",
    author='nathan todd-stone',
    author_email='me@nathants.com',
    python_requires='>=3.7',
    url='http://github.com/nathants/py-schema',
    install_requires=['py-util'],
    dependency_links=['https://github.com/nathants/py-util/tarball/fa60dbf761a61beb94614af89240fd5986d26786#egg=py-util-0.0.1'],
    packages=['schema'],
    description='data centric schema validation',
)
