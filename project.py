import sys
import socket
import threading
import time, datetime
from collections import deque

# global variables
LOCALHOST = '127.0.0.1' # loop-back 주소
lock = threading.Lock() # thread 동기화를 위한 lock
total_thread = 0 # 현재 실행되고 있는 client 스레드 개수
using_id = [] # 사용되고 있는 thread id 리스트
log_no = 1 # 터미널에 log가 출력될때의 seq number
image_filter = False # image filter 기능이 켜져있는가
logfile = open('log.txt', 'w') # cache log는 따로 파일에 남김

mb_size = 1048576 # 2^20
max_cache_size = int(float(sys.argv[2])*mb_size) # byte 단위
cur_cache_size = 0 # byte 단위
cache = {} # 형식: url => (respond list, status, mimetype, size(byte), counter)
deq = deque() # cache에 들어온 순으로 뒤에 추가됨. element형식: url
hit_no = 1 # log파일에 log가 입력될때의 seq number


try:
	# 현재 시각을 'xx:xx:xx.xxx'의 형식으로 반환
	def getcounter():
		now = str(datetime.datetime.now())
		return now.split(' ')[1][:12]

	# x bytes를 소수점 둘째자리까지의 MB 표현으로 바꾼다
	def byte_to_MB(x):
		return str(round(x/mb_size, 2))

	# 새로운 thread의 정보를 적용하고 할당될 thread의 id를 반환한다
	def inc_thread():
		global total_thread, using_id
		lock.acquire() # 전역 변수에 접근할때는 항상 lock을 이용해 동시 접근을 막음
		time.sleep(0.01)
		total_thread += 1

		# 현재 미사용중인 가장 작은 thread id를 찾는다
		thread_id = 1
		while thread_id in using_id:
			thread_id += 1
		using_id.append(thread_id)

		lock.release()
		return thread_id


	# 종료될 thread의 정보를 적용한다
	def dec_thread(thread_id):
		global total_thread, using_id
		lock.acquire()
		time.sleep(0.01)
		total_thread -= 1
		using_id.remove(thread_id)
		lock.release()


	# thread_id번 스레드가 통신을 마친 후 누적된 log를 한번에 출력한다
	def print_log(thread_id, log_list, url_filter):
		global total_thread, log_no, image_filter
		lock.acquire() # 여러 스레드의 터미널 출력 내용이 섞이면 안되므로 lock이 필요함
		time.sleep(0.01)
		print(str(log_no)+'  [Conn:  '+str(thread_id)+'/  '+str(total_thread)+']'+\
			' [Cache: '+byte_to_MB(cur_cache_size)+'/'+byte_to_MB(max_cache_size)+'MB'+']'+\
			' [Items: '+str(len(deq))+']')
		print('[ '+('O' if url_filter else 'X')+' ] URL filter', end=' ')
		print('| [ '+('O' if image_filter else 'X')+' ] Image filter')
		print()
		for log in log_list:
			print(log)
		print('-----------------------------------------------')
		log_no += 1
		lock.release()



	# 각 스레드에 할당되는 함수이다.
	# PRX의 입장에서 CLI와 SRV사이 http 통신의 중재역할을 한다.
	def prox_serve(cli_socket, cli_addr):
		global image_filter, max_cache_size, cur_cache_size, cache, deq, hit_no
		thread_id = inc_thread() # thread id 할당받기
		log_list = [] # 통신 내용을 담은 리스트
		cli_ip, cli_port = cli_addr

		###### CLI ==> PRX ######
		log_list.append('[CLI connected to '+str(cli_ip)+':'+str(cli_port)+']')
		request_raw = cli_socket.recv(999999) # CLI의 request 받기
		log_list.append('[CLI ==> PRX --- SRV]')

		request = str(request_raw)[2:-1] # b'data' 혹은 b"data" format에서 data를 추출 
		if not request: # CLI와의 연결이 끊긴 경우 connection drop
			cli_socket.close()
			dec_thread(thread_id)
			return

		lines = request.split('\\r\\n')
		firstline = lines[0] # request line
		method = firstline.split(' ')[0]
		consider = ['GET', 'HEAD', 'POST', 'PUT', 'DELETE']

		# 특정 형태의 method가 아니면 connection drop
		if method not in consider:
			cli_socket.close()
			dec_thread(thread_id)
			return

		# url에 image filter 쿼리가 있는지 확인
		url = firstline.split(' ')[1]
		if 'image_off' in url:
			image_filter = True
		elif 'image_on' in url:
			image_filter = False

		HOST = '' # host name of SRV
		userline = 'Not Specified' # User-Agent line

		# 위 2가지 정보를 request header에서 추출
		for line in lines:
			if 'Host:' in line:
				HOST = line.split(' ')[1]
			elif 'User-Agent:' in line:
				userline = line[12:]

		# Host line이 없으면 SRV를 알 수 없으므로 connection drop
		if not HOST:
			cli_socket.close()
			dec_thread(thread_id)
			return

		log_list.append('  > '+firstline)
		log_list.append('  > '+userline)

		###### PRX ==> SRV ######
		url_filter = False # url redirection이 되었는가
		# url 'yonsei'포함 => 'info.cern.ch' redirection
		# GET method에 대해서만 고려한다.
		if method == 'GET' and 'yonsei' in url:
			url_filter = True
			# url redirection에 따른 관련 정보 변경
			HOST = 'info.cern.ch'
			firstline = 'GET http://info.cern.ch HTTP/1.1'
			lines[0] = firstline
			for i in range(len(lines)):
				if 'Host:' in lines[i]:
					lines[i] = 'Host: '+HOST
					break
			url = 'http://info.cern.ch'
			request_raw = '\r\n'.join(lines).encode()
	
		# url filtering이 적용된 상태.
		# PRX-SRV connection을 활성화한다.
		prox_srv_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		try:
			prox_srv_socket.connect((HOST, 80))
			log_list.append('[SRV connected to '+str(HOST)+':'+str(80)+']')

		# SRV가 연결을 거부하면 connection drop
		except ConnectionRefusedError:
			cli_socket.close()
			prox_srv_socket.close()
			dec_thread(thread_id)
			return

		# PRX는 하나의 CLI request에 대해 SRV로부터 받은 splitted respond를
		# respond_list에 순차적으로 저장해둔다.
		respond_list = []
		status = 'Not Specified' # http status code
		mimetype = 'Not Specified' # content type
		size = 0 # entire size of respond

		# SRV에게 request하기 전에 proxy의 cache에 SRV의 respond정보가 이미 있는지 확인
		# GET method에 대해서만 cache를 이용한다.
		if method == 'GET' and url in cache:
			log_list.append('################## CACHE HIT ###################')
			lock.acquire()
			time.sleep(0.01)
			respond_list, status, mimetype, size, counter = cache[url]

			# log.txt에 현재 cache현황 기록하기
			logfile.write('# '+str(hit_no)+' cache hit\n')
			logfile.write(url+' '+byte_to_MB(size)+'MB\n')
			logfile.write('# start current situation\n')
			logfile.write('[Cache: '+byte_to_MB(cur_cache_size)+'/'+\
				byte_to_MB(max_cache_size)+'MB] [Items: '+str(len(deq))+']\n')

			len_deq = len(deq)
			for i in range(1, len_deq+1):
				keyurl = deq[i-1]
				logfile.write(str(i)+':data '+keyurl+'\n')
				logfile.write('   size '+byte_to_MB(cache[keyurl][3])+'MB\n')
				logfile.write('   counter '+cache[keyurl][4]+'\n')
			logfile.write('# end current situation\n\n')

			hit_no += 1
			lock.release()

		else: # cache정보를 이용할 수 없는 경우 SRV에게 직접 request해야한다.
			log_list.append('################## CACHE MISS ###################')
			###### PRX ==> SRV ######
			prox_srv_socket.send(request_raw) # SRV에 request 보내기
			log_list.append('[CLI --- PRX ==> SRV]')
			log_list.append('  > '+firstline)
			log_list.append('  > '+userline)

			###### PRX <== SRV ######
			while True:
				try:
					# SRV로부터 splitted respond를 받는다.
					respond_raw = prox_srv_socket.recv(999999)
					respond = str(respond_raw)[2:-1]
					if not respond: # SRV의 송신이 끝남
						break
					respond_list.append(respond_raw)
				
					# 첫번째로 받는 respond에는 header정보가 있음.
					# header로부터 status, mimetype, size정보를 추출한다.
					lines = respond.split('\\r\\n')
					if status == 'Not Specified':
						status = lines[0]
						status = status[len(status.split(' ')[0])+1:]

						for line in lines:
							if 'Content-Type:' in line:
								mimetype = line[len(line.split(' ')[0])+1:]
							elif 'Content-Length:' in line:
								size = int(line.split(' ')[1])

				# SRV측과 연결 불가시 connection drop
				except ConnectionResetError:
					cli_socket.close()
					prox_srv_socket.close()
					dec_thread(thread_id)
					return

			# PRX가 SRV의 respond를 온전히 받은 상태
			log_list.append('[CLI --- PRX <== SRV]')
			log_list.append('  > '+status)
			log_list.append('  > '+mimetype+' '+str(size)+'bytes')

			# cache에 저장할 수 있는 형태의 정보는 저장하기
			if method == 'GET' and 100 < size < max_cache_size:
				lock.acquire()
				time.sleep(0.01)

				# cache 공간 확보
				while cur_cache_size+size >= max_cache_size:
					# LRU replacement
					old_url = deq.popleft()
					old_size = cache[old_url][3]
					del cache[old_url]
					cur_cache_size -= old_size

					log_list.append('################## CACHE REMOVED ###################')
					log_list.append('  > '+old_url+' '+byte_to_MB(old_size)+'MB')
					log_list.append('  > '+'This file has been removed due to LRU!')

				# 확보된 공간에 새로운 정보 저장
				counter = getcounter()
				cache[url] = (respond_list, status, mimetype, size, counter)
				cur_cache_size += size
				deq.append(url)

				log_list.append('################## CACHE ADDED ###################')
				log_list.append('  > '+url+' '+byte_to_MB(size)+'MB')
				log_list.append('  > '+'This file has been added to the cache')
				log_list.append('  > '+'Current '+str(len(deq))+' items cached,  '+\
					byte_to_MB(cur_cache_size)+'/'+byte_to_MB(max_cache_size)+'MB')
				log_list.append('##################################################')
				lock.release()

		# PRX가 기존의 cache를 이용해서든, SRV에게 request를 보냈든
		# SRV에서 respond했던 data를 가지고 있는 상태.

		# CLI에게 forwarding하려는 정보가 image filtering에 걸리는 경우 connection drop
		if image_filter and 'image' in mimetype:
			cli_socket.close()
			prox_srv_socket.close()
			dec_thread(thread_id)
			return

		###### CLI <== PRX ######
		try: # CLI에게 splitted respond를 순차적으로 보낸다.
			for respond_raw in respond_list:
				cli_socket.send(respond_raw)

		# 도중에 전달 실패시 connection drop
		except BrokenPipeError:
			cli_socket.close()
			prox_srv_socket.close()
			dec_thread(thread_id)
			return
			
		# CLI에게 온전히 respond를 보낸 상태.
		log_list.append('[CLI <== PRX --- SRV]')
		log_list.append('  > '+status)
		log_list.append('  > '+mimetype+' '+str(size)+'bytes')

		# CLI의 request에 성공적으로 답했으므로 연결을 종료한다.
		cli_socket.close()
		log_list.append('[CLI disconnected]')
		prox_srv_socket.close()
		log_list.append('[SRV disconnected]')
		# 종료 직전 그동안 누적되었던 통신 내용 출력
		print_log(thread_id, log_list, url_filter)
		dec_thread(thread_id)
		return



	###### main ######
	PROX_PORT = int(sys.argv[1])
	# CLI측의 connection 요청을 accept하는 socket생성
	prox_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	# set feature socket reuse
	prox_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	prox_socket.bind((LOCALHOST, PROX_PORT))
	prox_socket.listen()

	print('Starting proxy server on port', PROX_PORT)
	print('-----------------------------------------------')

	while True: # ctrl+c가 발생할 때까지 CLI의 요청을 계속 받는다.
		cli_socket, cli_addr = prox_socket.accept()
		t = threading.Thread(target=prox_serve, args=(cli_socket, cli_addr))
		t.daemon = True
		t.start()

# ctrl+c가 입력되면 프록시 서버를 종료한다.
except KeyboardInterrupt:
	prox_socket.close()	
	logfile.close()

