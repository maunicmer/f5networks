#!/usr/bin/env python
# F5 Networks - Azure HA
# https://github.com/ArtiomL/f5networks
# Artiom Lichtenstein
# v0.9.4, 19/08/2016

from argparse import ArgumentParser
from datetime import timedelta
import json
import os
import requests
from signal import SIGKILL
import socket
from subprocess import call
import sys
from time import time

__author__ = 'Artiom Lichtenstein'
__license__ = 'MIT'
__version__ = '0.9.4'

# Log level to /var/log/ltm
intLogLevel = 0
strLogMethod = 'log'
strLogID = '[-v%s-160818-] %s - ' % (__version__, os.path.basename(sys.argv[0]))

# Azure RM REST API
class clsAREA:
	def __init__(self):
		self.strCFile = '/shared/tmp/scripts/azure/azure_ha.json'
		self.strMgmtURI = 'https://management.azure.com/'
		self.strAPIVer = '?api-version=2016-03-30'

	def funAbsURL(self):
		return self.strMgmtURI, self.strSubID, self.strRGName, self.strAPIVer

	def funURI(self, strMidURI):
		return self.strMgmtURI + strMidURI + self.strAPIVer

	def funBear(self):
		return { 'Authorization': 'Bearer %s' % self.strBearer }

objAREA = clsAREA()

# Logger command
strLogger = 'logger -p local0.'

# Exit codes
class clsExCodes:
	def __init__(self):
		self.args = 8
		self.armAuth = 4

objExCodes = clsExCodes()


def funLog(intMesLevel, strMessage, strSeverity='info'):
	if intLogLevel >= intMesLevel:
		if strLogMethod == 'stdout':
			print strMessage
		else:
			lstCmd = (strLogger + strSeverity).split(' ')
			lstCmd.append(strLogID + strMessage)
			call(lstCmd)


def funARMAuth():
	# Azure RM OAuth2
	global objAREA
	# Read external config file
	if not os.path.isfile(objAREA.strCFile):
		funLog(1, 'Credentials file: %s is missing!' % objAREA.strCFile, 'err')
		return 3

	try:
		# Open the credentials file
		with open(objAREA.strCFile, 'r') as f:
			diCreds = json.load(f)
		# Read subscription and resource group
		objAREA.strSubID = diCreds['subID']
		objAREA.strRGName = diCreds['rgName']
		# Current epoch time
		intEpNow = int(time())
		# Check if Bearer token exists (in credentials file) and whether it can be reused (expiration with 1 minute time skew)
		if (set(('bearer', 'expiresOn')) <= set(diCreds) and int(diCreds['expiresOn']) - 60 > intEpNow):
			objAREA.strBearer = diCreds['bearer'].decode('base64')
			funLog(2, 'Reusing existing Bearer, it expires in %s' % str(timedelta(seconds=int(diCreds['expiresOn']) - intEpNow)))
			return 0

		# Read additional config parameters
		strTenantID = diCreds['tenantID']
		strAppID = diCreds['appID']
		strPass = diCreds['pass'].decode('base64')
		strEndPt = 'https://login.microsoftonline.com/%s/oauth2/token' % strTenantID
	except Exception as e:
		funLog(1, 'Invalid credentials file: %s' % objAREA.strCFile, 'err')
		return 2

	# Generate new Bearer token
	diPayload = { 'grant_type': 'client_credentials', 'client_id': strAppID, 'client_secret': strPass, 'resource': objAREA.strMgmtURI }
	try:
		objHResp = requests.post(url=strEndPt, data=diPayload)
		diAuth = json.loads(objHResp.content)
		if 'access_token' in diAuth.keys():
			# Successfully received new token
			objAREA.strBearer = diAuth['access_token']
			# Write the new token and its expiration epoch into the credentials file
			diCreds['bearer'] = objAREA.strBearer.encode('base64')
			diCreds['expiresOn'] = diAuth['expires_on']
			with open(objAREA.strCFile, 'w') as f:
				f.write(json.dumps(diCreds, sort_keys=True, indent=4, separators=(',', ': ')))
			return 0

	except requests.exceptions.RequestException as e:
		funLog(2, str(e), 'err')
	return 1


def funLocIP(strRemIP):
	# Get local private IP
	objUDP = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	# The .connect method doesn't generate any real network traffic for UDP (socket.SOCK_DGRAM)
	objUDP.connect((strRemIP, 0))
	return objUDP.getsockname()[0]


def funCurState(strLocIP, strPeerIP):
	# Get current ARM state for the local machine
	global objAREA
	funLog(2, 'Current local private IP: %s, Resource Group: %s' % (strLocIP, objAREA.strRGName))
	# Construct loadBalancers URL
	strLBURL = '%ssubscriptions/%s/resourceGroups/%s/providers/Microsoft.Network/loadBalancers%s' % objAREA.funAbsURL()
	diHeaders = objAREA.funBear()
	try:
		# Get LBAZ JSON
		objHResp = requests.get(strLBURL, headers = diHeaders)
		# Store the backend pool JSON (for funFailover)
		objAREA.diBEPool = json.loads(objHResp.content)['value'][0]['properties']['backendAddressPools']
		# Extract backend IP ID ([1:] at the end removes the first "/" char)
		strBEIPURI = objAREA.diBEPool[0]['properties']['backendIPConfigurations'][0]['id'][1:]
		# Store the URI for NIC currently in the backend pool (for funFailover)
		objAREA.strCurNICURI = strBEIPURI.split('ipConfiguration')[0]
		# Get backend IP JSON
		objHResp = requests.get(objAREA.funURI(strBEIPURI), headers = diHeaders)
		# Extract private IP address
		strARMIP = json.loads(objHResp.content)['properties']['privateIPAddress']
		funLog(2, 'Current private IP in Azure RM: %s' % strARMIP)
		if strARMIP == strLocIP:
			# This machine is already Active
			funLog(1, 'Current state: Active')
			return 'Active'

		elif strARMIP == strPeerIP:
			# The dead peer is listed as Active - failover required
			return 'Standby'

	except Exception as e:
		funLog(2, str(e), 'err')
	funLog(1, 'Current state: Unknown', 'warning')
	return 'Unknown'


def funOpStatus(objHResp):
	strStatus = 'InProgress'
	strOpURL = objHResp.headers['Azure-AsyncOperation']
	funLog(2, 'ARM Async Operation, x-ms-request-id: %s' % objHResp.headers['x-ms-request-id'])
	diHeaders = objAREA.funBear()
	while strStatus == 'InProgress':
		try:
			strStatus = json.loads(requests.get(strOpURL, headers = diHeaders).content)['status']
			funLog(3, 'ARM Async Operation Status: %s' % strStatus)
		except Exception as e:
			funLog(2, str(e), 'err')
			break
	funLog(2, strStatus)
	return strStatus


def funFailover():
	diHeaders = objAREA.funBear()
	try:
		strOldNICURL = objAREA.funURI(objAREA.strCurNICURI)
	except AttributeError as e:
		funLog(2, 'No NICs in the Backend Pool!', 'warning')
		return 3

	strChar = 'B/'
	if objAREA.strCurNICURI.endswith('B/'):
		strChar = 'A/'
	strNewNICURL = objAREA.funURI(objAREA.strCurNICURI[:-2] + strChar)
	try:
		# Get the JSON of the NIC currently in the backend pool
		objHResp = requests.get(strOldNICURL, headers = diHeaders)
		diOldNIC = json.loads(objHResp.content)
		# Remove the LB backend pool from that JSON
		diOldNIC['properties']['ipConfigurations'][0]['properties']['loadBalancerBackendAddressPools'] = []
		# Get the JSON of the new NIC to be added to the backend pool
		objHResp = requests.get(strNewNICURL, headers = diHeaders)
		diNewNIC = json.loads(objHResp.content)
		# Remove the existing backend IP ID from the LB backend pool JSON (stored in funCurState)
		objAREA.diBEPool[0]['properties']['backendIPConfigurations'] = []
		# Add the LB backend pool to the new NIC JSON
		diNewNIC['properties']['ipConfigurations'][0]['properties']['loadBalancerBackendAddressPools'] = objAREA.diBEPool
		# Add Content-Type to HTTP headers
		diHeaders['Content-Type'] = 'application/json'
		# Update the new NIC (add it to the backend pool)
		objHResp = requests.put(strNewNICURL, headers = diHeaders, data = json.dumps(diNewNIC))
		funLog(1, 'Adding the new NIC to LBAZ BE Pool...')
		if funOpStatus(objHResp) != 'Succeeded':
			return 2

		# Update the old NIC (remove it from the backend pool)
		objHResp = requests.put(strOldNICURL, headers = diHeaders, data = json.dumps(diOldNIC))
		funLog(1, 'Removing the old NIC from LBAZ BE Pool... ')
		if funOpStatus(objHResp) == 'Succeeded':
			return 0

	except Exception as e:
		funLog(2, str(e), 'err')
	return 1


def funArgParse():
	objArgParse = ArgumentParser()
	objArgParse.add_argument('-a', help='test Azure RM authentication and exit', action='store_true', dest='auth')
	objArgParse.add_argument('-l', help='set log level (0-3)', action='store', type=int, dest='level')
	objArgParse.add_argument('-s', help='log to stdout (instead of /var/log/ltm)', action='store_true', dest='sout')
	objArgParse.add_argument('-v', action='version', version='%(prog)s ' + __version__)
	objArgs, unknown = objArgParse.parse_known_args()
	return objArgs


def main():
	global strLogMethod, intLogLevel
	objArgs = funArgParse()
	if objArgs.sout:
		strLogMethod = 'stdout'
	if objArgs.level > 0:
		intLogLevel = objArgs.level
	if objArgs.auth:
		strLogMethod = 'stdout'
		sys.exit(funARMAuth())

	funLog(1, '=' * 62)
	if len(sys.argv) < 3:
		funLog(1, 'Not enough arguments!', 'err')
		sys.exit(objExCodes.args)

	# Remove IPv6/IPv4 compatibility prefix (LTM passes addresses in IPv6 format)
	strRIP = sys.argv[1].strip(':f')
	strRPort = sys.argv[2]
	# PID file
	strPFile = '_'.join(['/var/run/', os.path.basename(sys.argv[0]), strRIP, strRPort + '.pid'])
	# PID
	strPID = str(os.getpid())

	funLog(2, 'PIDFile: %s, PID: %s' % (strPFile, strPID))

	# Kill the last instance of this monitor if hung
	if os.path.isfile(strPFile):
		try:
			os.kill(int(file(strPFile, 'r').read()), SIGKILL)
			funLog(1, 'Killed the last hung instance of this monitor.', 'warning')
		except OSError:
			pass

	# Record current PID
	file(strPFile, 'w').write(str(os.getpid()))

	# Health monitor
	try:
		objHResp = requests.head(''.join(['https://', strRIP, ':', strRPort]), verify = False)
		if objHResp.status_code == 200:
			os.unlink(strPFile)
			# Any standard output stops the script from running. Clean up any temporary files before the standard output operation
			funLog(2, 'Peer: %s is up.' % strRIP)
			print 'UP'
			sys.exit()

	except requests.exceptions.RequestException as e:
		funLog(2, str(e), 'err')

	# Peer down, ARM action required
	funLog(1, 'Peer down, ARM action required.', 'warning')
	if funARMAuth() != 0:
		funLog(1, 'ARM Auth Error!', 'err')
		os.unlink(strPFile)
		sys.exit(objExCodes.armAuth)

	# ARM Auth OK
	funLog(3, 'ARM Bearer: %s' % objAREA.strBearer)

	if funCurState(funLocIP(strRIP), strRIP) == 'Standby':
		funLog(1, 'We\'re Standby in ARM, Active peer down. Trying to failover...', 'warning')
		funFailover()

	os.unlink(strPFile)
	sys.exit(1)

if __name__ == '__main__':
	main()